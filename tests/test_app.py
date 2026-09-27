from unittest import mock

import pytest
from click.testing import CliRunner

from aws_ssh_utils import connection
from aws_ssh_utils.commands.app import app as app_command
from aws_ssh_utils.commands.app import group_role, group_sort_key
from aws_ssh_utils.commands.healthcheck import healthcheck
from aws_ssh_utils.connection import Connection, Environment, Target, connect
from aws_ssh_utils.ssh import cli
from aws_ssh_utils.terminal import key_to_input, pyte_color

TARGET = Target(instance_id='i-123', ip='10.0.0.1', user='ec2-user', key_name='my-key', opkssh_provider='issuer,client')


def test_connect_tries_every_strategy_in_order_and_returns_checklist():
    """All strategies fail: every attempt is made in spec order and reported."""
    env = Environment(ssm_installed=True, opkssh_installed=True)
    calls = []

    def fail(hostname, username, key_files, **kwargs):
        calls.append((hostname, list(key_files), kwargs['use_defaults'], kwargs.get('sock') is not None))
        raise OSError('nope')

    with (
        mock.patch.object(connection, 'find_ssh_config', return_value=None),
        mock.patch.object(connection, 'find_ssh_key_file', return_value='/k/my-key.pem'),
        mock.patch.object(connection, 'valid_opkssh_key_file', return_value=None),
        mock.patch.object(connection, 'opkssh_login', return_value='/k/opkssh'),
        mock.patch.object(connection, 'proxy_socket', return_value=mock.Mock()),
        mock.patch.object(connection, 'open_ssh', side_effect=fail),
    ):
        result = connect(TARGET, env, on_output=lambda _: None, on_waiting=lambda: None)

    assert calls == [
        ('i-123', ['/k/my-key.pem'], True, True),  # SSM
        ('10.0.0.1', ['/k/opkssh'], False, False),  # opkssh
        ('10.0.0.1', ['/k/my-key.pem'], False, False),  # EC2 key
        ('10.0.0.1', [], True, False),  # default keys
    ]
    assert isinstance(result, list)
    assert [line.split('\n')[0] for line in result] == [
        ":x: Failed to start session with AWS session-manager",
        ":x: Failed to authenticate with opkssh",
        ":x: Key found, but failed - ssh -o IdentitiesOnly=yes -i /k/my-key.pem ec2-user@10.0.0.1",
        ":x: Failed with default key - ssh ec2-user@10.0.0.1",
    ]


def test_connect_stops_at_first_success_and_reports_missing_tools():
    """Missing SSM is skipped, first working strategy wins."""
    env = Environment(ssm_installed=False, opkssh_installed=False)
    client = mock.Mock()
    with (
        mock.patch.object(connection, 'find_ssh_config', return_value=None),
        mock.patch.object(connection, 'find_ssh_key_file', return_value='/k/my-key.pem'),
        mock.patch.object(connection, 'valid_opkssh_key_file', return_value=None),
        mock.patch.object(connection, 'open_ssh', return_value=client) as open_ssh,
    ):
        assert connect(TARGET, env, on_output=lambda _: None, on_waiting=lambda: None) == Connection(client, 'my-key')

    open_ssh.assert_called_once_with('10.0.0.1', 'ec2-user', ['/k/my-key.pem'], use_defaults=False)


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
        pytest.param([], {'profile': None, 'region': None}, id='no-args'),
        pytest.param(['--verbose'], {'profile': None, 'region': None}, id='group-flag-only'),
        pytest.param(['-p', 'prod', '-r', 'eu-west-1'], {'profile': 'prod', 'region': 'eu-west-1'}, id='app-options'),
        pytest.param(['--quiet', 'app', '-p', 'prod'], {'profile': 'prod', 'region': None}, id='explicit'),
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
