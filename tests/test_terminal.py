import asyncio
import threading
from unittest import mock

import pytest
from paramiko import Channel, Message
from textual import events
from textual._xterm_parser import XTermParser
from textual.app import App
from textual.color import Color
from textual.filter import Monochrome

from aws_ssh_utils.terminal import ShellApp, Terminal


@pytest.mark.parametrize('focus_return', ['before-paste', 'after-paste', 'missing'])
def test_paste_after_confirmation_dialog_reaches_shell(focus_return):
    """An external paste dialog may deliver its paste before restoring focus."""
    channel = mock.Mock()
    channel.send.side_effect = len
    app = ShellApp('SSH', mock.Mock(), channel)

    async def run():
        with mock.patch.object(Terminal, 'read_channel'):
            async with app.run_test() as pilot:
                terminal = app.query_one(Terminal)
                terminal.feed(b'\x1b[?2004h')
                assert app.focused is terminal
                app.post_message(events.Paste('small'))
                await pilot.pause()
                channel.send.assert_called_once_with(b'\x1b[200~small\x1b[201~')
                channel.send.reset_mock()

                app.post_message(events.AppBlur())
                await pilot.pause()
                assert app.focused is None
                if focus_return == 'before-paste':
                    app.post_message(events.AppFocus())
                app.post_message(events.Paste('x' * 8192))
                if focus_return == 'after-paste':
                    app.post_message(events.AppFocus())
                await pilot.pause()

                channel.send.assert_called_once_with(b'\x1b[200~' + b'x' * 8192 + b'\x1b[201~')
                assert app.focused is terminal
                await pilot.press('z')
                assert channel.send.call_args == mock.call(b'z')

    asyncio.run(run())


@pytest.mark.parametrize('bracketed', [False, True])
@pytest.mark.parametrize('repeats', [1024, 16384])
def test_large_paste_survives_ssh_packet_limits(bracketed, repeats):
    """Use Paramiko's real send implementation, including its packet-size limit."""
    channel = Channel(0)
    channel.transport = mock.Mock()
    channel.transport._sanitize_packet_size.side_effect = lambda size: size
    channel._set_remote_channel(1, 2**24, 32768)
    terminal = Terminal(mock.Mock(), channel)
    if bracketed:
        terminal.feed(b'\x1b[?2004h')

    text = 'hello \u00e9\U0001f642\r\n' * repeats
    # Exercise Textual's parser too, with a paste spanning multiple input reads.
    parser = XTermParser()
    wire = f'\x1b[200~{text}\x1b[201~'
    parsed = []
    for offset in range(0, len(wire), 4096):
        parsed.extend(parser.feed(wire[offset : offset + 4096]))
    assert len(parsed) == 1
    assert isinstance(parsed[0], events.Paste)
    assert parsed[0].text == text
    asyncio.run(terminal.on_paste(parsed[0]))

    packets = []
    for call in channel.transport._send_user_message.call_args_list:
        message = Message(call.args[0].asbytes())
        message.get_byte()
        message.get_int()
        packets.append(message.get_binary())
    expected = f'\x1b[200~{text}\x1b[201~' if bracketed else text
    assert b''.join(packets) == expected.encode()
    assert all(len(packet) <= 32768 for packet in packets)


def test_paste_keeps_app_responsive_and_following_keys_in_order():
    channel = mock.Mock()
    received = bytearray()
    writing = threading.Event()
    output_processed = threading.Event()

    def send(data):
        writing.set()
        assert output_processed.wait(timeout=5), 'Paste blocked the app loop'
        received.extend(data[:1024])
        return min(len(data), 1024)

    channel.send.side_effect = send
    terminal = Terminal(mock.Mock(), channel)
    terminal.read_channel = mock.Mock()

    class TestApp(App):
        def compose(self):
            yield terminal

    async def run():
        async with TestApp().run_test() as pilot:
            terminal.feed(b'\x1b[?2004h')
            terminal.post_message(events.Paste('x' * 65536))
            terminal.post_message(events.Key('z', 'z'))
            try:
                assert await asyncio.to_thread(writing.wait, 5)
                terminal.feed(b'Output while pasting')
                assert terminal.vt.display[0].startswith('Output while pasting')
            finally:
                output_processed.set()
            await pilot.pause()
            assert received == b'\x1b[200~' + b'x' * 65536 + b'\x1b[201~z'

    asyncio.run(run())


@pytest.mark.parametrize('failure', [0, OSError('Disconnected')])
def test_paste_closes_session_if_write_fails(failure):
    channel = mock.Mock()
    channel.send.side_effect = [2, failure]
    terminal = Terminal(mock.Mock(), channel)
    with mock.patch.object(terminal, 'post_message') as post:
        asyncio.run(terminal.on_paste(events.Paste('hello')))
    assert channel.send.call_count == 2
    assert isinstance(post.call_args.args[0], Terminal.Closed)


def test_blank_rows_during_resize_can_be_rendered_without_color():
    terminal = Terminal(mock.Mock(), mock.Mock())
    # Layout can request new rows before the Resize event updates pyte.
    strip = terminal.render_line(terminal.vt.lines)
    strip.apply_filter(Monochrome(), Color(0, 0, 0))


@pytest.mark.parametrize('split', range(6))
def test_vim_keyboard_query_preserves_styles_and_terminal_io(split):
    """Vim's modifyOtherKeys query must not kill output, even across recv calls."""
    channel = mock.Mock()
    channel.send.side_effect = len
    terminal = Terminal(mock.Mock(), channel)
    terminal.feed(b'\x1b[1;31mBefore ')
    attrs = terminal.vt.cursor.attrs

    query = b'\x1b[?4m'
    terminal.feed(query[:split])
    terminal.feed(query[split:] + b'after')
    assert terminal.vt.cursor.attrs == attrs
    assert terminal.vt.display[0].rstrip() == 'Before after'
    # Ignoring a capability we don't implement must not claim support for it.
    channel.send.assert_not_called()

    terminal.feed(b'\r\n\x1b[0mStill connected')
    assert terminal.vt.display[1].rstrip() == 'Still connected'
    assert terminal.vt.cursor.attrs.fg == 'default'
    assert not terminal.vt.cursor.attrs.bold
    terminal.on_key(events.Key('escape', '\x1b'))
    terminal.on_key(events.Key(':', ':'))
    terminal.on_key(events.Key('q', 'q'))
    terminal.on_key(events.Key('enter', '\r'))
    assert channel.send.call_args_list == [mock.call(data) for data in (b'\x1b', b':', b'q', b'\r')]


@pytest.mark.parametrize('parser_error', [False, True])
def test_reader_updates_after_vim_or_closes_session_on_parser_failure(parser_error):
    """Exercise the real worker: a dead reader must never leave a frozen panel."""
    client = mock.Mock()
    channel = mock.Mock()
    channel.recv.side_effect = [b'Vim', b'\x1b[?4m', b'\r\nShell prompt', b'']
    terminal = Terminal(client, channel)
    if parser_error:
        terminal.stream.feed = mock.Mock(side_effect=TypeError('unsupported terminal query'))

    class TestApp(App):
        def __init__(self):
            super().__init__()
            self.closed = asyncio.Event()

        def compose(self):
            yield terminal

        async def on_terminal_closed(self):
            await terminal.remove()
            self.closed.set()

    async def run():
        app = TestApp()
        async with app.run_test():
            await asyncio.wait_for(app.closed.wait(), timeout=5)
            client.close.assert_called_once()
            if not parser_error:
                assert terminal.vt.display[1].rstrip() == 'Shell prompt'
                assert channel.recv.call_count == 4

    asyncio.run(run())
