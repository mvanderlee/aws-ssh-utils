# Source: https://github.com/paramiko/paramiko/blob/main/demos/interactive.py

# Copyright (C) 2003-2007  Robey Pointer <robeypointer@gmail.com>
#
# This file is part of paramiko.
#
# Paramiko is free software; you can redistribute it and/or modify it under the
# terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# Paramiko is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Paramiko; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA.
import os
import socket
import sys
from importlib.util import find_spec

import paramiko
from loguru import logger

# https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h2-Bracketed-Paste-Mode
START_PASTE = "\x1B\x5B\x32\x30\x30\x7E"  # ESC[200~
END_PASTE = "\x1B\x5B\x32\x30\x31\x7E"  # ESC[201~


ALL_CODECS = [
    'ascii', 'big5', 'big5hkscs', 'cp037', 'cp273', 'cp424', 'cp437',
    'cp500', 'cp720', 'cp737', 'cp775', 'cp850', 'cp852', 'cp855', 'cp856', 'cp857',
    'cp858', 'cp860', 'cp861', 'cp862', 'cp863', 'cp864', 'cp865', 'cp866', 'cp869',
    'cp874', 'cp875', 'cp932', 'cp949', 'cp950', 'cp1006', 'cp1026', 'cp1125',
    'cp1140', 'cp1250', 'cp1251', 'cp1252', 'cp1253', 'cp1254', 'cp1255', 'cp1256',
    'cp1257', 'cp1258', 'euc_jp', 'euc_jis_2004', 'euc_jisx0213', 'euc_kr',
    'gb2312', 'gbk', 'gb18030', 'hz', 'iso2022_jp', 'iso2022_jp_1', 'iso2022_jp_2',
    'iso2022_jp_2004', 'iso2022_jp_3', 'iso2022_jp_ext', 'iso2022_kr', 'latin_1',
    'iso8859_2', 'iso8859_3', 'iso8859_4', 'iso8859_5', 'iso8859_6', 'iso8859_7',
    'iso8859_8', 'iso8859_9', 'iso8859_10', 'iso8859_11', 'iso8859_13',
    'iso8859_14', 'iso8859_15', 'iso8859_16', 'johab', 'koi8_r', 'koi8_t', 'koi8_u',
    'kz1048', 'mac_cyrillic', 'mac_greek', 'mac_iceland', 'mac_latin2', 'mac_roman',
    'mac_turkish', 'ptcp154', 'shift_jis', 'shift_jis_2004', 'shift_jisx0213',
    'utf_32', 'utf_32_be', 'utf_32_le', 'utf_16', 'utf_16_be', 'utf_16_le', 'utf_7',
    'utf_8', 'utf_8_sig',
]

# windows does not have termios...
has_termios = find_spec("termios") is not None and find_spec("tty") is not None


_TITLE_START = '\x1bk' if os.getenv('TMUX') else '\x1b]0;'
_TITLE_END = '\x1b\\' if os.getenv('TMUX') else '\x07'


def is_int(val: str) -> bool:
    try:
        int(val)
        return True
    except Exception:
        return False


def remove_title_change(data: str) -> str:
    '''
        Title change is a string starting with '\x1b]0;', and ending with '\x07'.
        Remove all such substrings
    '''
    while True:
        a = data.find(_TITLE_START)
        b = data.find(_TITLE_END)

        if a == -1 or b == -1:
            break

        data = data[:a] + data[b + len(_TITLE_END):]

    return data


def interactive_shell(chan: paramiko.Channel, allow_title_changes: bool = True):
    if has_termios:
        posix_shell(chan, allow_title_changes=allow_title_changes)
    else:
        windows_shell(chan, allow_title_changes=allow_title_changes)


def decode(chars: bytes) -> str:
    '''
        Decodes the bytes and handles encoding errors
        `htop` scrolling for example uses cp037
    '''
    try:
        # Attempt to decode the entire input as UTF-8
        return chars.decode('utf-8')
    except Exception:
        # Attempt to decode each individual character
        def decode_char(byte: int) -> str:
            for codec in ALL_CODECS:
                try:
                    c = byte.to_bytes(1, 'big').decode(codec)
                    return c
                except Exception:
                    logger.debug(
                        f'Failed to decode character {byte} as {codec}',
                    )
            else:
                # Failed to decode character, return replacement character.
                # https://www.fileformat.info/info/unicode/char/fffd/index.htm
                return '\uFFFD'

        return ''.join(decode_char(b) for b in chars)


def posix_readkey() -> str:
    """Get a keypress. If an escaped key is pressed, the full sequence is
    read and returned.

        Copied from readchar:
        https://github.com/magmax/python-readchar/blob/master/readchar/_posix_read.py#L30
    """

    def read():
        '''
            Reads one character and handles encoding errors
            `htop` scrolling for example uses cp037
        '''
        return decode(sys.stdin.buffer.raw.read(1))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownArgumentType]

    c1 = read()

    if c1 != "\x1B":  # ESC
        return c1

    c2 = read()
    if c2 not in "\x4F\x5B":  # O[
        return c1 + c2

    c3 = read()
    if c3 not in "\x31\x32\x33\x35\x36":  # 12356
        return c1 + c2 + c3

    c4 = read()
    if c4 not in "\x30\x31\x33\x34\x35\x37\x38\x39":  # 01345789
        return c1 + c2 + c3 + c4

    c5 = read()
    key = c1 + c2 + c3 + c4 + c5

    # Bracketed Paste Mode: # https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h2-Bracketed-Paste-Mode
    if key == START_PASTE[:-1] or key == END_PASTE[:-1]:
        c6 = read()
        return key + c6

    return key


def windows_readkey() -> str:
    """Reads the next keypress. If an escaped key is pressed, the full
    sequence is read and returned.

        Copied from readchar:
        https://github.com/magmax/python-readchar/blob/master/readchar/_win_read.py#LL14C1-L30C24
    """

    ch = sys.stdin.read(1)

    # if it is a normal character:
    if ch not in "\x00\xe0":
        return ch

    # if it is a scpeal key, read second half:
    ch2 = sys.stdin.read(1)

    return "\x00" + ch2


def posix_shell(chan: paramiko.Channel, allow_title_changes: bool = True):
    if not has_termios:
        raise RuntimeError("Termios is not available on this system")

    import select
    import termios
    import tty

    oldtty = termios.tcgetattr(sys.stdin)  # pyright: ignore[reportAttributeAccessIssue]

    # input_history = []
    # output_history = []

    try:
        tty.setraw(sys.stdin.fileno())  # pyright: ignore[reportAttributeAccessIssue]
        tty.setcbreak(sys.stdin.fileno())  # pyright: ignore[reportAttributeAccessIssue]
        chan.settimeout(0.0)
        while True:
            r, _, _ = select.select([chan, sys.stdin], [], [])
            if chan in r:
                try:
                    data = decode(chan.recv(1024))
                    if len(data) == 0:
                        sys.stdout.write("\r\n")
                        break

                    if not allow_title_changes:
                        data = remove_title_change(data)

                    # output_history.append(data)
                    sys.stdout.write(data)
                    sys.stdout.flush()
                except TimeoutError:
                    pass
            if sys.stdin in r:
                key = posix_readkey()
                # When pasting something, we need to read the entire pasted blob at once
                # Otherwise it'll hang until the next key press.
                # This has to do with how 'select.select' detects changes.
                # A paste is a single event of many characters, so we must handle them all as one event
                if key == START_PASTE:
                    # Start reading the pasted text
                    key = posix_readkey()
                    # Until we reach the end of the pasted text
                    while key != END_PASTE:
                        chan.send(key.encode())
                        # input_history.append(key)
                        key = posix_readkey()
                    # We've exhausted the paste event, wait for next event
                    continue

                if len(key) == 0:
                    break
                chan.send(key.encode())
                # input_history.append(key)

    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, oldtty)  # pyright: ignore[reportAttributeAccessIssue]

    # Useful in debugging how control characters were send
    # from pprint import pprint
    # pprint(input_history)
    # pprint(output_history)


# thanks to Mike Looijmans for this code
def windows_shell(chan: paramiko.Channel, allow_title_changes: bool = True):
    import threading

    sys.stdout.write(
        "Line-buffered terminal emulation. Press F6 or ^Z to send EOF.\r\n\r\n",
    )

    def writeall(sock: socket.socket):
        while True:
            data = sock.recv(256).decode()
            if not data:
                # Need user to input any character so we sys.stdin.read(1) completes and unblocks
                sys.stdout.write(
                    "\r\n Connection closed. Press Enter to continue...\r\n",
                )
                sys.stdout.flush()
                break

            if not allow_title_changes:
                data = remove_title_change(data)

            sys.stdout.write(data)
            sys.stdout.flush()

    writer = threading.Thread(target=writeall, args=(chan,))
    writer.start()

    try:
        while True:
            d = windows_readkey()
            if not d or chan.closed:
                break
            chan.send(d.encode())
    except EOFError:
        # user hit ^Z or F6
        pass
