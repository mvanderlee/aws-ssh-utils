import asyncio
import shlex
from unittest import mock

import pytest
from click.testing import CliRunner
from textual.app import App
from textual.widgets import RichLog, Static, TabbedContent

from aws_ssh_utils import connection
from aws_ssh_utils.cli_utils import render_attempts
from aws_ssh_utils.commands.app import ShellPane, SSHApp
from aws_ssh_utils.commands.app import app as app_command
from aws_ssh_utils.commands.healthcheck import healthcheck
from aws_ssh_utils.connection import Attempt, Connection, Environment, Target, connect
from aws_ssh_utils.emr_utils import group_role, group_sort_key
from aws_ssh_utils.ssh import cli
from aws_ssh_utils.terminal import ShellStatus, Terminal, key_to_input, pyte_color

TARGET = Target(instance_id='i-123', ip='10.0.0.1', user='ec2-user', key_name='my-key', opkssh_provider='issuer,client')


def test_switching_shell_tabs_restores_keyboard_input():
    app = SSHApp(mock.Mock(), mock.Mock(), Environment(False, False), 100)
    channels = [mock.Mock(), mock.Mock()]

    async def run():
        with (
            mock.patch.object(app, 'load_ec2'),
            mock.patch.object(app, 'load_emr'),
            mock.patch.object(ShellPane, 'connect'),
            mock.patch.object(Terminal, 'read_channel'),
        ):
            async with app.run_test(size=(140, 40)) as pilot:
                for index, channel in enumerate(channels):
                    app.open_shell(str(index), lambda: TARGET)
                    await pilot.pause()
                    pane = app.query_one(f'#shell-{index}', ShellPane)
                    await pane.attach('SSH', Terminal(mock.Mock(), channel))
                    await pilot.pause()

                shells = app.query_one('#shells', TabbedContent)
                for index in (0, 1, 0):
                    shells.active = f'shell-{index}'
                    await pilot.pause()
                    terminal = shells.active_pane.query_one(Terminal)
                    assert app.focused is terminal
                    channels[index].send.reset_mock()
                    await pilot.press('x')
                    channels[index].send.assert_called_once_with(b'x')

    asyncio.run(run())


def test_shell_attach_stops_animation_and_updates_label_without_attempts():
    pane = ShellPane('Test shell', lambda: TARGET, Environment(False, False), 0, id='shell-test')
    terminal = Terminal(mock.Mock(), mock.Mock())

    class TestApp(App):
        def compose(self):
            with TabbedContent():
                yield pane

    async def run():
        with mock.patch.object(pane, 'connect'), mock.patch.object(terminal, 'read_channel'):
            async with TestApp().run_test():
                attempts = [Attempt('SSH', 'ssh example', mock.Mock(), state='connecting')]
                pane.set_attempts(attempts)
                assert str(pane.query_one('.attempts', Static).render()) == str(render_attempts(attempts))
                with mock.patch.object(pane.animation, 'stop', wraps=pane.animation.stop) as stop:
                    original_remove = RichLog.remove

                    def remove_log(log):
                        # The timer must stop before the first removal yields control.
                        stop.assert_called_once()
                        return original_remove(log)

                    with mock.patch.object(RichLog, 'remove', remove_log):
                        await pane.attach('SSH', terminal)

                assert pane.status == 'connected'
                assert not pane.query('.attempts')
                assert not pane.query(RichLog)
                assert pane.query_one(Terminal) is terminal
                assert pane.query_one(ShellStatus).via == 'SSH'
                pane.update_label()
                assert pane.query_ancestor(TabbedContent).get_tab(pane).label.plain == '● Test shell'

    asyncio.run(run())


def test_connect_tries_every_strategy_in_order_and_reports_progress():
    """All strategies fail: every attempt is made in spec order and reported."""
    env = Environment(ssm_installed=True, opkssh_installed=True)
    calls = []
    progress = []

    def fail(hostname, username, key_files, **kwargs):
        calls.append((hostname, list(key_files), kwargs['use_defaults'], kwargs.get('sock') is not None))
        raise OSError('nope')

    with (
        mock.patch.object(connection, 'find_ssh_config', return_value=None),
        mock.patch.object(connection, 'find_ssh_key_file', return_value='/k/my-key.pem'),
        mock.patch.object(connection, 'valid_opkssh_key_file', return_value=None),
        mock.patch.object(connection, 'opkssh_login', return_value='/k/opkssh'),
        mock.patch.object(connection.SSMSession, 'open', side_effect=OSError('nope')) as open_ssm,
        mock.patch.object(connection, 'open_ssh', side_effect=fail),
    ):
        result = connect(TARGET, env, on_output=lambda _: None, on_waiting=lambda: None, on_progress=progress.append)

    assert calls == [
        ('10.0.0.1', ['/k/opkssh'], False, False),  # opkssh
        ('10.0.0.1', ['/k/my-key.pem'], False, False),  # EC2 key
        ('10.0.0.1', [], True, False),  # default keys
    ]
    assert result is None
    open_ssm.assert_called_once_with(['aws', 'ssm', 'start-session', '--target', 'i-123'])
    opkssh_key = shlex.quote(str(connection.opkssh_key_path('issuer,client')))
    assert progress[0][0].state == 'connecting'
    assert progress[1][0].state == 'failed'
    assert str(render_attempts(progress[-1])).splitlines() == [
        "❌ Failed to connect via AWS SSM - nope",
        "    aws ssm start-session --target i-123",
        "❌ Failed to connect via opkssh - nope",
        (
            f"    opkssh login --provider issuer,client -i {opkssh_key}"
            f" && ssh -o IdentitiesOnly=yes -i {opkssh_key} ec2-user@10.0.0.1"
        ),
        "❌ Failed to connect via my-key - nope",
        "    ssh -o IdentitiesOnly=yes -i /k/my-key.pem ec2-user@10.0.0.1",
        "❌ Failed to connect via default key - nope",
        "    ssh ec2-user@10.0.0.1",
    ]


def test_connect_stops_at_first_success_and_reports_missing_tools():
    """Missing tools are skipped, first working strategy wins."""
    env = Environment(ssm_installed=False, opkssh_installed=False)
    client = mock.Mock()
    progress = []
    with (
        mock.patch.object(connection, 'find_ssh_config', return_value=None),
        mock.patch.object(connection, 'find_ssh_key_file', return_value='/k/my-key.pem'),
        mock.patch.object(connection, 'valid_opkssh_key_file', return_value=None),
        mock.patch.object(connection, 'open_ssh', return_value=client) as open_ssh,
    ):
        result = connect(TARGET, env, on_output=lambda _: None, on_waiting=lambda: None, on_progress=progress.append)

    assert result == Connection(client, 'my-key')
    assert str(render_attempts(progress[-1])).splitlines() == [
        "  AWS CLI or session-manager-plugin not installed, skipping.",
        "  opkssh not installed, skipping.",
        "✓ Connected via my-key",
        "  default key",
    ]

    open_ssh.assert_called_once_with('10.0.0.1', 'ec2-user', ['/k/my-key.pem'], use_defaults=False, confirm_host_key=None)


@pytest.mark.parametrize(
    ('key', 'character', 'app_cursor', 'expected'),
    [
        pytest.param('a', 'a', False, 'a', id='printable'),
        pytest.param('ctrl+c', '\x03', False, '\x03', id='control'),
        pytest.param('ctrl+p', None, False, '\x10', id='control-without-character'),
        pytest.param('enter', '\r', False, '\r', id='enter'),
        pytest.param('up', None, False, '\x1b[A', id='arrow'),
        pytest.param('up', None, True, '\x1bOA', id='arrow-app-cursor'),
        pytest.param('delete', None, True, '\x1b[3~', id='delete-app-cursor'),
    ],
)
def test_key_to_input(key, character, app_cursor, expected):
    assert key_to_input(key, character, app_cursor) == expected


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        pytest.param('default', None, id='default'),
        pytest.param('brown', 'yellow', id='brown'),
        pytest.param('brightred', 'bright_red', id='bright'),
        pytest.param('ff00aa', '#ff00aa', id='hex'),
    ],
)
def test_pyte_color(name, expected):
    assert pyte_color(name) == expected


def test_emr_groups_sort_master_core_task():
    groups = ['Task - 2', 'Core Instance Group', 'Custom', 'MasterFleet', 'Task - 1']
    assert sorted(groups, key=group_sort_key) == ['MasterFleet', 'Core Instance Group', 'Task - 1', 'Task - 2', 'Custom']
    assert [group_role(g) for g in ('Primary', 'CORE', 'task')] == ['M', 'C', 'T']


@pytest.mark.parametrize(
    ('args', 'expected_params'),
    [
        pytest.param([], {'profile': None, 'region': None, 'scrollback': None}, id='no-args'),
        pytest.param(['--verbose'], {'profile': None, 'region': None, 'scrollback': None}, id='group-flag-only'),
        pytest.param(
            ['-p', 'prod', '-r', 'eu-west-1', '--scrollback', '50'],
            {'profile': 'prod', 'region': 'eu-west-1', 'scrollback': 50},
            id='app-options',
        ),
        pytest.param(['--quiet', 'app', '-p', 'prod'], {'profile': 'prod', 'region': None, 'scrollback': None}, id='explicit'),
    ],
)
def test_app_is_the_default_command(args, expected_params):
    with mock.patch.object(app_command, 'callback') as callback:
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    callback.assert_called_once_with(**expected_params)


def test_other_commands_and_help_still_work():
    runner = CliRunner()
    assert 'Commands:' in runner.invoke(cli, ['--help']).output
    assert 'Usage: cli app' in runner.invoke(cli, ['-p', 'prod', '--help']).output
    assert 'No such command' in runner.invoke(cli, ['eec2']).output
    with mock.patch.object(healthcheck, 'callback') as callback:
        assert runner.invoke(cli, ['healthcheck']).exit_code == 0
    callback.assert_called_once()
