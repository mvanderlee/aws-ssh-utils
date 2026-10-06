"""Textual widgets rendering SSH and SSM shell channels through the pyte VT100 emulator."""

import asyncio
import datetime as dt
import time
from collections import deque
from collections.abc import Sequence
from functools import lru_cache
from importlib.metadata import version
from typing import Any

import pyte
from loguru import logger
from pyte.screens import Char, Margins
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static
from typing_extensions import override

from aws_ssh_utils.logging_utils import LoggedApp
from aws_ssh_utils.session import ShellChannel, ShellClient

KEYS = {
    'enter': '\r',
    'tab': '\t',
    'shift+tab': '\x1b[Z',
    'backspace': '\x7f',
    'escape': '\x1b',
    'up': '\x1b[A',
    'down': '\x1b[B',
    'right': '\x1b[C',
    'left': '\x1b[D',
    'home': '\x1b[H',
    'end': '\x1b[F',
    'insert': '\x1b[2~',
    'delete': '\x1b[3~',
    'pageup': '\x1b[5~',
    'pagedown': '\x1b[6~',
    'f1': '\x1bOP',
    'f2': '\x1bOQ',
    'f3': '\x1bOR',
    'f4': '\x1bOS',
    'f5': '\x1b[15~',
    'f6': '\x1b[17~',
    'f7': '\x1b[18~',
    'f8': '\x1b[19~',
    'f9': '\x1b[20~',
    'f10': '\x1b[21~',
    'f11': '\x1b[23~',
    'f12': '\x1b[24~',
}
SCROLL_KEYS = {'shift+pageup': 1, 'shift+pagedown': -1}
"""Pages to scroll back per key press."""
WHEEL_LINES = 3
ERASE_SCROLLBACK = 3
"""`CSI 3 J`, sent by e.g. `clear`."""
# pyte stores private (DEC) modes shifted by 5 bits
APPLICATION_CURSOR = 1 << 5
BRACKETED_PASTE = 2004 << 5


def key_to_input(key: str, character: str | None, app_cursor: bool = False) -> str | None:
    """Translate a Textual key into the bytes a VT100 terminal would send."""
    if seq := KEYS.get(key):
        # Application cursor mode (vim, less, ...) expects SS3 instead of CSI for arrows/home/end
        if app_cursor and len(seq) == 3 and seq[1] == '[':
            return f'\x1bO{seq[2]}'
        return seq
    if character is None and len(key) == 6 and key.startswith('ctrl+') and key[-1].isalpha():
        return chr(ord(key[-1]) & 0x1F)
    return character


def pyte_color(name: str) -> str | None:
    """pyte uses names like 'brown'/'brightred' and bare hex for 256/true color."""
    if name == 'default':
        return None
    if len(name) == 6 and all(c in '0123456789abcdefABCDEF' for c in name):
        return f'#{name}'
    if name.startswith('bright'):
        name = f'bright_{name[6:]}'
    return name.replace('brown', 'yellow')


@lru_cache(maxsize=1024)
def cell_style(
    fg: str,
    bg: str,
    bold: bool,
    italics: bool,
    underscore: bool,
    strikethrough: bool,
    reverse: bool,
) -> Style:
    return Style(
        color=pyte_color(fg),
        bgcolor=pyte_color(bg),
        bold=bold,
        italic=italics,
        underline=underscore,
        strike=strikethrough,
        reverse=reverse,
    )


class ChannelScreen(pyte.Screen):
    """Answers terminal queries over the channel, and keeps the lines scrolled off the top as history."""

    def __init__(self, channel: ShellChannel, columns: int, lines: int, scrollback: int = 0):
        super().__init__(columns, lines)
        self.channel = channel
        self.history: deque[tuple[Char, ...]] = deque(maxlen=scrollback)
        # Interned so history lines share Char objects instead of each holding a dict of its own.
        self._chars: dict[Char, Char] = {}

    @override
    def write_process_input(self, data: str):
        self.channel.send(data.encode())

    @override
    def select_graphic_rendition(self, *attrs: int, private: bool = False):
        # Vim queries modifyOtherKeys with CSI ? 4 m. pyte 0.8.2 dispatches it
        # here, but its SGR handler rejects `private`, killing the reader worker.
        # Ignore unsupported keyboard queries without changing text attributes.
        if not private:
            super().select_graphic_rendition(*attrs)

    @override
    def index(self):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        # Only full-screen scrolls; lines leaving a scroll region (e.g. below a status line) aren't history.
        if self.history.maxlen and top == 0 and self.cursor.y == bottom:
            row = self.buffer[top]
            self.history.append(tuple(self._chars.setdefault(row[x], row[x]) for x in range(self.columns)))
        super().index()

    @override
    def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any):
        if how == ERASE_SCROLLBACK:
            self.history.clear()
        super().erase_in_display(how, *args, **kwargs)


class Terminal(Widget, can_focus=True):
    DEFAULT_CSS = """
    Terminal {
        height: 1fr;
    }
    """

    class Closed(Message):
        pass

    def __init__(self, client: ShellClient, channel: ShellChannel, scrollback: int = 0, **kwargs: object):
        super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]
        self.client = client
        self.channel = channel
        self.vt = ChannelScreen(channel, 80, 24, scrollback)
        self.stream = pyte.ByteStream(self.vt)
        self.scrolled = 0
        """How many history lines the view is scrolled back."""

    def on_mount(self):
        self.read_channel()
        self.focus()

    def on_unmount(self):
        self.client.close()

    @work(thread=True, exit_on_error=False)
    def read_channel(self):
        try:
            while data := self.channel.recv(65536):
                self.app.call_from_thread(self.feed, data)
        except OSError:
            logger.exception('Terminal channel read failed')
        except Exception:
            logger.exception('Terminal reader failed')
            raise
        finally:
            # Unexpected parser errors must also tear down the dead session.
            # Textual records the worker exception, but exit_on_error=False
            # would otherwise leave the last frame looking like a live terminal.
            self.post_message(self.Closed())

    def feed(self, data: bytes):
        history_size = len(self.vt.history)
        self.stream.feed(data)
        if self.scrolled:
            # Keep a scrolled back view still while output arrives.
            # ponytail: drifts once history is full, as the deque then drops a line per new one.
            self.scroll_history(len(self.vt.history) - history_size)
        self.refresh()

    def scroll_history(self, lines: int):
        self.scrolled = max(0, min(self.scrolled + lines, len(self.vt.history)))
        self.refresh()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp):
        event.stop()
        self.scroll_history(WHEEL_LINES)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown):
        event.stop()
        self.scroll_history(-WHEEL_LINES)

    def on_resize(self, event: events.Resize):
        width, height = event.size.width, event.size.height
        if width and height:
            self.vt.resize(height, width)
            self.channel.resize_pty(width=width, height=height)

    def on_key(self, event: events.Key):
        if event.key in SCROLL_KEYS:
            event.stop()
            event.prevent_default()
            self.scroll_history(SCROLL_KEYS[event.key] * (self.vt.lines - 1))
            return

        data = key_to_input(event.key, event.character, app_cursor=APPLICATION_CURSOR in self.vt.mode)
        if data:
            event.stop()
            event.prevent_default()
            self.send(data)

    async def on_paste(self, event: events.Paste):
        event.stop()
        text = event.text
        started = time.monotonic()
        logger.bind(file_only=True).info(
            'Paste shell received: chars={}; bracketed={}',
            len(text),
            BRACKETED_PASTE in self.vt.mode,
        )
        if BRACKETED_PASTE in self.vt.mode:
            text = f'\x1b[200~{text}\x1b[201~'
        if self.scrolled:
            self.scroll_history(-self.scrolled)
        try:
            # SSH flow control may block a large paste. Keep the app loop free
            # so the reader can deliver output while input is still being sent.
            await asyncio.to_thread(self._send_all, text.encode())
            logger.bind(file_only=True).info(
                'Paste shell sent: bytes={}; elapsed={:.3f}s',
                len(text.encode()),
                time.monotonic() - started,
            )
        except OSError:
            logger.exception('Terminal paste failed')
            self.post_message(self.Closed())

    def send(self, text: str):
        """Send user input, jumping back to the live screen like other terminals do."""
        if self.scrolled:
            self.scroll_history(-self.scrolled)
        self._send_all(text.encode())

    def _send_all(self, data: bytes):
        """A channel write may accept only one SSH packet or the available window."""
        while data:
            sent = self.channel.send(data)
            if sent <= 0:
                raise OSError('Terminal channel closed while sending input')
            data = data[sent:]

    @override
    def render_line(self, y: int) -> Strip:
        vt = self.vt
        if y >= vt.lines:
            # Layout may grow before on_resize updates pyte. Textual's color
            # filters require a Style even for these temporary blank rows.
            return Strip.blank(self.size.width, Style())

        index = len(vt.history) - self.scrolled + y
        if index < len(vt.history):
            line = vt.history[index]
            row: Sequence[Char] = [line[x] if x < len(line) else vt.default_char for x in range(vt.columns)]
            cursor_x = -1
        else:
            y = index - len(vt.history)
            buffer_row = vt.buffer[y]
            row = [buffer_row[x] for x in range(vt.columns)]
            cursor_x = vt.cursor.x if self.has_focus and not vt.cursor.hidden and y == vt.cursor.y else -1

        segments = []
        for x, char in enumerate(row):
            style = cell_style(*char[1:8])
            if x == cursor_x:
                style += Style(reverse=not char.reverse)
            segments.append(Segment(char.data, style))
        return Strip(segments).simplify()


class ShellStatus(Horizontal):
    """How and when the shell connected, and for how long."""

    DEFAULT_CSS = """
    ShellStatus {
        height: 1;
        padding: 0 1;
        background: $surface;
        & > .via { width: 1fr; }
        & > .duration { width: auto; }
    }
    """

    def __init__(self, via: str):
        super().__init__()
        self.via = via
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        connected_at = dt.datetime.now().isoformat(sep=' ', timespec='seconds')
        yield Static(Text(f"Connected via {self.via} at {connected_at}"), classes='via')
        yield Static(classes='duration')

    def on_mount(self):
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self):
        minutes, seconds = divmod(int(time.monotonic() - self.started), 60)
        hours, minutes = divmod(minutes, 60)
        self.query_one('.duration', Static).update(f"{hours:02}:{minutes:02}:{seconds:02}")


class TerminalApp(LoggedApp):
    """Shared input handling for apps containing interactive shell terminals."""

    # Its priority ctrl+p binding would steal shell history navigation from the terminal.
    ENABLE_COMMAND_PALETTE = False

    def on_mount(self):
        logger.bind(file_only=True).info('Terminal input diagnostics v1; textual={}', version('textual'))

    @override
    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, (events.AppFocus, events.AppBlur)):
            logger.bind(file_only=True).info('Terminal focus event: {}', type(event).__name__)
        if isinstance(event, events.Paste) and not event.is_forwarded and not self.app_focus:
            # A terminal's paste confirmation dialog can send the paste before
            # FocusIn. Textual restores the previous widget for keys/mouse, but
            # not pastes, so they otherwise go to an unfocused screen and vanish.
            self.app_focus = True
        if isinstance(event, events.Paste) and not event.is_forwarded:
            logger.bind(file_only=True).info(
                'Paste app received: chars={}; app_focus={}; target={}',
                len(event.text),
                self.app_focus,
                type(self.focused).__name__,
            )
        await super().on_event(event)


class ShellApp(TerminalApp):
    """A single full-screen shell, exits when the shell closes."""

    def __init__(self, via: str, client: ShellClient, channel: ShellChannel, scrollback: int = 0):
        super().__init__()
        self.via = via
        self.client = client
        self.channel = channel
        self.scrollback = scrollback

    def compose(self) -> ComposeResult:
        yield ShellStatus(self.via)
        yield Terminal(self.client, self.channel, self.scrollback)

    def on_terminal_closed(self):
        self.exit()
