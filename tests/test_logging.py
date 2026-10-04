import asyncio
import importlib
import threading
from unittest import mock

import pytest
from click.testing import CliRunner
from loguru import logger

from aws_ssh_utils.commands.app import app as app_command
from aws_ssh_utils.logging_utils import LoggedApp, terminal_logging
from aws_ssh_utils.ssh import cli
from aws_ssh_utils.terminal import Terminal


@pytest.mark.parametrize('command', ['implicit', 'explicit', 'equals'])
def test_log_file_option_and_debug_logging(tmp_path, command):
    log_file = tmp_path / 'session.log'
    args = ['--verbose', '--log-file', str(log_file)]
    if command == 'explicit':
        args.append('app')
    elif command == 'equals':
        args = ['--verbose', f'--log-file={log_file}']
    args += ['--profile', 'prod']

    def run(**kwargs):
        logger.debug('connection diagnostic')
        logger.info('session started')

    with mock.patch.object(app_command, 'callback', side_effect=run) as callback:
        result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert callback.call_args.kwargs['profile'] == 'prod'
    output = log_file.read_text(encoding='utf-8')
    assert 'connection diagnostic' in output
    assert 'session started' in output
    assert 'source=' in output
    assert '\x1b' not in output


def test_environment_log_file_appends_even_when_console_is_quiet(tmp_path):
    log_file = tmp_path / 'session.log'
    log_file.write_text('previous run\n', encoding='utf-8')
    with mock.patch.object(app_command, 'callback', side_effect=lambda **_: logger.info('new run')):
        result = CliRunner().invoke(cli, ['--quiet', '--verbose'], env={'AWS_SSH_LOG_FILE': str(log_file)})
    assert result.exit_code == 0, result.output
    assert result.output == ''
    assert log_file.read_text(encoding='utf-8').startswith('previous run\n')
    assert 'new run' in log_file.read_text(encoding='utf-8')


def test_app_start_does_not_remove_file_logging(tmp_path):
    app_module = importlib.import_module('aws_ssh_utils.commands.app')
    log_file = tmp_path / 'session.log'
    with (
        mock.patch.object(app_module.boto3, 'Session'),
        mock.patch.object(app_module, 'detect_environment'),
        mock.patch.object(app_module.SSHApp, 'run', side_effect=lambda: logger.error('UI failure')),
    ):
        result = CliRunner().invoke(cli, ['--log-file', str(log_file), '--scrollback', '0'])
    assert result.exit_code == 0, result.output
    assert 'UI failure' in log_file.read_text(encoding='utf-8')
    assert 'UI failure' not in result.output


def test_worker_logging_does_not_write_over_full_screen_ui(tmp_path):
    log_file = tmp_path / 'session.log'

    def run(**kwargs):
        with terminal_logging():
            thread = threading.Thread(target=lambda: logger.error('background failure'))
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
        logger.info('console restored')

    with mock.patch.object(app_command, 'callback', side_effect=run):
        result = CliRunner().invoke(cli, ['--log-file', str(log_file)])
    assert result.exit_code == 0, result.output
    assert 'background failure' not in result.output
    assert 'console restored' in result.output
    assert 'background failure' in log_file.read_text(encoding='utf-8')


@pytest.mark.parametrize('failure', ['ui', 'reader'])
def test_full_screen_failures_write_tracebacks(tmp_path, failure):
    log_file = tmp_path / 'session.log'
    terminal = Terminal(mock.Mock(), mock.Mock())
    terminal.channel.recv.side_effect = [b'private-shell-output-must-not-be-logged', b'']
    terminal.stream.feed = mock.Mock(side_effect=ValueError('reader test failure'))

    class FailingApp(LoggedApp):
        def compose(self):
            if failure == 'reader':
                yield terminal

        def on_mount(self):
            if failure == 'ui':
                raise ValueError('UI test failure')

        def on_terminal_closed(self):
            self.exit()

    async def run_app():
        with terminal_logging():
            async with FailingApp().run_test() as pilot:
                await pilot.pause()

    def run(**kwargs):
        if failure == 'ui':
            with pytest.raises(ValueError, match='UI test failure'):
                asyncio.run(run_app())
        else:
            asyncio.run(run_app())

    with mock.patch.object(app_command, 'callback', side_effect=run):
        result = CliRunner().invoke(cli, ['--log-file', str(log_file)])
    assert result.exit_code == 0, result.output
    output = log_file.read_text(encoding='utf-8')
    assert 'Traceback (most recent call last)' in output
    assert 'ValueError:' in output
    assert 'Unhandled FailingApp error' in output if failure == 'ui' else 'Terminal reader failed' in output
    assert 'private-shell-output-must-not-be-logged' not in output


def test_invalid_log_path_fails_before_launch(tmp_path):
    parent = tmp_path / 'file'
    parent.write_text('not a directory', encoding='utf-8')
    with mock.patch.object(app_command, 'callback') as callback:
        result = CliRunner().invoke(cli, ['--log-file', str(parent / 'session.log')])
    assert result.exit_code != 0
    assert 'Cannot open log file' in result.output
    callback.assert_not_called()


def test_missing_log_file_value_is_reported():
    result = CliRunner().invoke(cli, ['--log-file'])
    assert result.exit_code != 0
    assert 'requires an argument' in result.output
