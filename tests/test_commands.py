from unittest import mock

import paramiko
import pytest
from click.testing import CliRunner

from aws_ssh_utils import connection
from aws_ssh_utils.commands import ec2 as ec2_command
from aws_ssh_utils.commands import emr as emr_command
from aws_ssh_utils.connection import AcceptNewPolicy, Target
from aws_ssh_utils.ssh import cli


def questions(questionary: mock.Mock) -> list[tuple[str, str]]:
    """The (kind, prompt) of each question asked, in order."""
    return [(c[0], c.args[0]) for c in questionary.mock_calls if c[0] in ('text', 'select', 'confirm')]


def test_ec2_asks_the_same_questions_and_opens_a_shell():
    """Name filter, then instance choice. The chosen instance is connected to with the detected user."""
    ec2_client = mock.Mock()
    ec2_client.get_paginator.return_value.paginate.return_value = [
        {
            'Reservations': [
                {
                    'Instances': [
                        {
                            'InstanceId': 'i-1',
                            'PrivateIpAddress': '10.0.0.1',
                            'ImageId': 'ami-1',
                            'KeyName': 'k',
                            'Tags': [
                                {'Key': 'Name', 'Value': 'web'},
                            ],
                        },
                        {'InstanceId': 'i-2', 'PrivateIpAddress': '10.0.0.2', 'Tags': [{'Key': 'Name', 'Value': 'db'}]},
                    ],
                },
            ],
        },
    ]
    ec2_client.describe_images.return_value = {'Images': [{'Name': 'ubuntu-jammy'}]}
    questionary = mock.Mock()
    questionary.text.return_value.unsafe_ask.return_value = 'WE'
    questionary.select.return_value.unsafe_ask.return_value = 'web'

    with (
        mock.patch.object(ec2_command.boto3, 'Session') as session,
        mock.patch('aws_ssh_utils.ec2_utils.questionary', questionary),
        mock.patch.object(ec2_command, 'open_shell') as open_shell,
        mock.patch.object(ec2_command, 'detect_environment'),
    ):
        session.return_value.client.return_value = ec2_client
        result = CliRunner().invoke(cli, ['ec2', '-p', 'prod'])

    assert result.exit_code == 0, result.output
    assert questions(questionary) == [
        ('text', 'Provide a name filter or leave blank to show all'),
        ('select', 'Which EC2 instance do you want to connect to?'),
    ]
    assert questionary.select.call_args.kwargs['choices'] == ['web']
    target = open_shell.call_args.args[0]
    assert target == Target(instance_id='i-1', ip='10.0.0.1', user='ubuntu', aliases=('web',), key_name='k')
    assert open_shell.call_args.kwargs['title'] == 'ubuntu@web'


def emr_client() -> mock.Mock:
    emr = mock.Mock()
    emr.list_clusters.return_value = {'Clusters': [{'Id': 'j-1', 'Name': 'etl', 'Status': {}}]}
    emr.describe_cluster.return_value = {
        'Cluster': {'InstanceCollectionType': 'INSTANCE_GROUP', 'Ec2InstanceAttributes': {'Ec2KeyName': 'missing-key'}},
    }
    emr.list_instance_groups.return_value = {
        'InstanceGroups': [{'Id': 'ig-m', 'Name': 'Master Instance Group'}, {'Id': 'ig-c', 'Name': 'Core Instance Group'}],
    }
    emr.list_instances.return_value = {
        'Instances': [
            {'InstanceGroupId': 'ig-m', 'Ec2InstanceId': 'i-m', 'PrivateIpAddress': '10.1.0.1'},
            {'InstanceGroupId': 'ig-c', 'Ec2InstanceId': 'i-c2', 'PrivateIpAddress': '10.1.0.3'},
            {'InstanceGroupId': 'ig-c', 'Ec2InstanceId': 'i-c1', 'PrivateIpAddress': '10.1.0.2'},
        ],
    }
    return emr


@pytest.mark.parametrize(
    ('continue_without_key', 'exit_code'),
    [
        pytest.param(True, 0, id='continue'),
        pytest.param(False, 1, id='abort'),
    ],
)
def test_emr_asks_the_same_questions_and_opens_a_shell(continue_without_key, exit_code):
    """Cluster, group, instance, then whether to continue without the cluster's key."""
    questionary = mock.Mock()
    questionary.select.return_value.unsafe_ask.side_effect = ['etl - j-1 - None', 'Core Instance Group', '10.1.0.3']
    questionary.confirm.return_value.unsafe_ask.return_value = continue_without_key

    with (
        mock.patch.object(emr_command.boto3, 'Session') as session,
        mock.patch('aws_ssh_utils.emr_utils.questionary', questionary),
        mock.patch.object(emr_command, 'questionary', questionary),
        mock.patch.object(emr_command, 'find_ssh_key_file', return_value=None),
        mock.patch.object(emr_command, 'open_shell') as open_shell,
        mock.patch.object(
            emr_command,
            'detect_environment',
            return_value=connection.Environment(ssm_installed=False, opkssh_installed=False),
        ),
    ):
        session.return_value.client.return_value = emr_client()
        result = CliRunner().invoke(cli, ['emr'])

    assert result.exit_code == exit_code, result.output
    assert questions(questionary) == [
        ('select', 'Which cluster do you want to connect to?'),
        ('select', 'Which instance group do you want to connect to?'),
        ('select', 'Which instance do you want to connect to?'),
        ('confirm', 'Could not find the ssh key missing-key, would you like to continue?'),
    ]
    assert questionary.select.call_args.kwargs['choices'] == ['10.1.0.2', '10.1.0.3']
    if not continue_without_key:
        open_shell.assert_not_called()
        return

    target, _env = open_shell.call_args.args
    assert target == Target(instance_id='i-c2', ip='10.1.0.3', user='hadoop', key_name='missing-key')
    assert open_shell.call_args.kwargs == {
        'title': 'etl - Core[1]',
        'scrollback': None,
        'initial_input': emr_command.CORE_NODE_INPUT,
    }


def test_emr_does_not_ask_for_an_ssh_key_when_ssm_is_available():
    questionary = mock.Mock()
    questionary.select.return_value.unsafe_ask.side_effect = ['etl - j-1 - None', 'Core Instance Group', '10.1.0.3']
    env = connection.Environment(ssm_installed=True, opkssh_installed=False)
    with (
        mock.patch.object(emr_command.boto3, 'Session') as session,
        mock.patch('aws_ssh_utils.emr_utils.questionary', questionary),
        mock.patch.object(emr_command, 'questionary', questionary),
        mock.patch.object(emr_command, 'find_ssh_key_file') as find_key,
        mock.patch.object(emr_command, 'open_shell') as open_shell,
        mock.patch.object(emr_command, 'detect_environment', return_value=env),
    ):
        session.return_value.client.return_value = emr_client()
        result = CliRunner().invoke(cli, ['emr'])

    assert result.exit_code == 0, result.output
    questionary.confirm.assert_not_called()
    find_key.assert_not_called()
    assert open_shell.call_args.args[1] is env


@pytest.mark.parametrize(
    ('confirmed', 'trusted'),
    [
        pytest.param(None, True, id='no-confirm-accepts-new'),
        pytest.param(True, True, id='confirmed'),
        pytest.param(False, False, id='declined'),
    ],
)
def test_unknown_host_keys_are_saved_unless_declined(tmp_path, confirmed, trusted):
    known_hosts = tmp_path / 'known_hosts'
    known_hosts.touch()
    key = mock.Mock(spec=paramiko.PKey)
    key.get_name.return_value = 'ssh-ed25519'
    key.get_base64.return_value = 'AAAA'
    client = mock.Mock()
    policy = AcceptNewPolicy(known_hosts, 22, None if confirmed is None else lambda host, k: confirmed)

    if trusted:
        policy.missing_host_key(client, 'i-123', key)
        assert known_hosts.read_text() == 'i-123 ssh-ed25519 AAAA\n'
    else:
        with pytest.raises(paramiko.SSHException):
            policy.missing_host_key(client, 'i-123', key)
        assert known_hosts.read_text() == ''


def test_explicit_key_file_replaces_key_lookup():
    target = Target(instance_id='i-1', ip='10.0.0.1', user='ec2-user', key_name='k', key_file='/explicit/mine.pem')
    env = connection.Environment(ssm_installed=False, opkssh_installed=False)
    with (
        mock.patch.object(connection, 'find_ssh_config', return_value=None),
        mock.patch.object(connection, 'find_ssh_key_file') as find_key,
        mock.patch.object(connection, 'open_ssh') as open_ssh,
    ):
        result = connection.connect(target, env, on_output=lambda _: None, on_waiting=lambda: None, on_progress=lambda _: None)

    find_key.assert_not_called()
    assert result == connection.Connection(open_ssh.return_value, 'mine')
