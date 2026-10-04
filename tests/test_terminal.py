import asyncio
from unittest import mock

import pytest
from textual import events
from textual.app import App
from textual.color import Color
from textual.filter import Monochrome

from aws_ssh_utils.terminal import Terminal


def test_blank_rows_during_resize_can_be_rendered_without_color():
    terminal = Terminal(mock.Mock(), mock.Mock())
    # Layout can request new rows before the Resize event updates pyte.
    strip = terminal.render_line(terminal.vt.lines)
    strip.apply_filter(Monochrome(), Color(0, 0, 0))


@pytest.mark.parametrize('split', range(6))
def test_vim_keyboard_query_preserves_styles_and_terminal_io(split):
    """Vim's modifyOtherKeys query must not kill output, even across recv calls."""
    channel = mock.Mock()
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
