import queue
import sys
import threading
from unittest import mock

import pytest

from aws_ssh_utils import connection, session
from aws_ssh_utils.connection import Environment, Target
from aws_ssh_utils.session import SSMSession


class FakePty:
    def __init__(self, *output):
        self.output = queue.Queue()
        for item in output:
            self.output.put(item)
        self.write = mock.Mock()
        self.setwinsize = mock.Mock()
        self.close = mock.Mock(side_effect=lambda **_: self.output.put(EOFError()))

    def read(self, size):
        item = self.output.get(timeout=5)
        if isinstance(item, Exception):
            raise item
        return item


def test_ssm_connects_without_keys_when_no_ssh_config_matches():
    env = Environment(True, False, profile='truedevs', region='eu-west-1')
    target = Target('i-123', '', 'ec2-user')
    progress = []
    with (
        mock.patch.object(connection.SSMSession, 'open') as open_ssm,
        mock.patch.object(connection, 'find_ssh_config', return_value=None) as config,
        mock.patch.object(connection, 'find_ssh_key_file') as keys,
        mock.patch.object(connection, 'open_ssh') as ssh,
    ):
        result = connection.connect(target, env, lambda _: None, lambda: None, progress.append)

    open_ssm.assert_called_once_with(
        [
            'aws',
            'ssm',
            'start-session',
            '--target',
            'i-123',
            '--profile',
            'truedevs',
            '--region',
            'eu-west-1',
        ],
    )
    config.assert_called_once_with(target)
    keys.assert_not_called()
    ssh.assert_not_called()
    assert result == connection.Connection(open_ssm.return_value, 'AWS SSM')
    assert progress[-1][0].state == 'connected'


@pytest.mark.parametrize('config_succeeds', [True, False])
def test_ssh_config_is_tried_before_keyless_ssm(config_succeeds):
    target = Target('i-123', '', 'ec2-user')
    env = Environment(True, False)
    options = {'hostname': 'my-host'}
    progress = []
    calls = mock.Mock()
    with (
        mock.patch.object(connection, 'find_ssh_config', return_value=('my-host', options)),
        mock.patch.object(connection, 'open_with_config') as open_config,
        mock.patch.object(connection.SSMSession, 'open') as open_ssm,
        mock.patch.object(connection, 'find_ssh_key_file') as keys,
    ):
        calls.attach_mock(open_config, 'config')
        calls.attach_mock(open_ssm, 'ssm')
        if not config_succeeds:
            open_config.side_effect = OSError('SSH unavailable')
        result = connection.connect(target, env, lambda _: None, lambda: None, progress.append)

    open_config.assert_called_once_with('my-host', options, target, None)
    keys.assert_not_called()
    assert [attempt.via for attempt in progress[-1]] == ['ssh config (my-host)', 'AWS SSM']
    if config_succeeds:
        open_ssm.assert_not_called()
        assert result == connection.Connection(open_config.return_value, 'ssh config (my-host)')
    else:
        assert [call[0] for call in calls.mock_calls] == ['config', 'ssm']
        assert result == connection.Connection(open_ssm.return_value, 'AWS SSM')
        assert [attempt.state for attempt in progress[-1]] == ['failed', 'connected']


def test_ssm_preserves_split_startup_output_unicode_and_eof():
    process = FakePty('\x1b[0mStarting session with Ses', 'sionId: example\r\n', 'héllo\r\n', EOFError())
    with mock.patch.object(session, 'spawn_pty', return_value=process):
        client = SSMSession.open(['aws'])
    try:
        assert connection.open_shell_channel(client) is client
        output = bytearray()
        while data := client.recv(3):
            output.extend(data)
        assert output.decode() == '\x1b[0mStarting session with SessionId: example\r\nhéllo\r\n'
        assert client.recv(3) == b''
        client.send('é\x03'.encode())
        process.write.assert_called_once_with('é\x03')
        client.resize_pty(width=120, height=40)
        process.setwinsize.assert_called_once_with(40, 120)
    finally:
        client.close()
        client.close()
    process.close.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    ('output', 'error', 'message'),
    [
        (['AccessDeniedException: not authorized\r\n', EOFError()], OSError, 'AccessDeniedException'),
        ([EOFError()], OSError, 'SSM exited before starting'),
        ([OSError('broken terminal')], OSError, 'broken terminal'),
        ([], TimeoutError, 'Timed out starting SSM'),
    ],
)
def test_ssm_startup_failures_close_process(output, error, message):
    process = FakePty(*output)
    with mock.patch.object(session, 'spawn_pty', return_value=process):
        with pytest.raises(error, match=message):
            SSMSession.open(['aws'], timeout=0.1)
    process.close.assert_called_once_with(force=True)


def test_closing_ssm_unblocks_terminal_reader():
    process = FakePty('Starting session with SessionId: example\r\n')
    with mock.patch.object(session, 'spawn_pty', return_value=process):
        client = SSMSession.open(['aws'])
    client.recv(65536)
    received = queue.Queue()
    reader = threading.Thread(target=lambda: received.put(client.recv(65536)), daemon=True)
    reader.start()
    client.close()
    assert received.get(timeout=2) == b''
    reader.join(timeout=2)
    assert not reader.is_alive()


def test_real_pty_input_output_resize_and_close():
    """Exercise the installed OS backend without AWS credentials or a remote instance."""
    command = [
        sys.executable,
        '-u',
        '-c',
        (
            "import os, sys; "
            "print('Starting session with SessionId: local-test', flush=True); "
            "line = input(); "
            "print('REPLY=' + line, flush=True); "
            "print('SIZE=%s,%s' % tuple(os.get_terminal_size()), flush=True); "
            "input()"
        ),
    ]
    client = SSMSession.open(command, timeout=10)
    received = queue.Queue()

    def read():
        while data := client.recv(65536):
            received.put(data)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        client.resize_pty(width=100, height=35)
        client.send(b'hello\r')
        output = b''
        while b'SIZE=100,35' not in output:
            output += received.get(timeout=10)
        assert b'REPLY=hello' in output
    finally:
        client.close()
        reader.join(timeout=5)
    assert not reader.is_alive()
