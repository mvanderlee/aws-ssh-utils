"""Textual widget rendering a paramiko shell channel through the pyte VT100 emulator."""

from functools import lru_cache

import paramiko
import pyte
from rich.segment import Segment
from rich.style import Style
from textual import events, work
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from typing_extensions import override

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
    """Answers terminal queries, such as cursor position reports, over the channel."""

    def __init__(self, channel: paramiko.Channel, columns: int, lines: int):
        super().__init__(columns, lines)
        self.channel = channel

    @override
    def write_process_input(self, data: str):
        self.channel.send(data.encode())


class Terminal(Widget, can_focus=True):
    DEFAULT_CSS = """
    Terminal {
        height: 1fr;
    }
    """

    class Closed(Message):
        pass

    def __init__(self, client: paramiko.SSHClient, channel: paramiko.Channel, **kwargs: object):
        super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]
        self.client = client
        self.channel = channel
        self.vt = ChannelScreen(channel, 80, 24)
        self.stream = pyte.ByteStream(self.vt)

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
            pass
        self.post_message(self.Closed())

    def feed(self, data: bytes):
        self.stream.feed(data)
        self.refresh()

    def on_resize(self, event: events.Resize):
        width, height = event.size.width, event.size.height
        if width and height:
            self.vt.resize(height, width)
            self.channel.resize_pty(width=width, height=height)

    def on_key(self, event: events.Key):
        data = key_to_input(event.key, event.character, app_cursor=APPLICATION_CURSOR in self.vt.mode)
        if data:
            event.stop()
            event.prevent_default()
            self.channel.send(data.encode())

    def on_paste(self, event: events.Paste):
        text = event.text
        if BRACKETED_PASTE in self.vt.mode:
            text = f'\x1b[200~{text}\x1b[201~'
        self.channel.send(text.encode())

    @override
    def render_line(self, y: int) -> Strip:
        vt = self.vt
        if y >= vt.lines:
            return Strip.blank(self.size.width)

        row = vt.buffer[y]
        cursor_x = vt.cursor.x if self.has_focus and not vt.cursor.hidden and y == vt.cursor.y else -1
        segments = []
        for x in range(vt.columns):
            char = row[x]
            style = cell_style(*char[1:8])
            if x == cursor_x:
                style += Style(reverse=not char.reverse)
            segments.append(Segment(char.data, style))
        return Strip(segments).simplify()
