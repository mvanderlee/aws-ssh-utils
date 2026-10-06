import json
from unittest import mock

import pytest

from aws_ssh_utils import cli_utils
from aws_ssh_utils.cli_utils import detect_scrollback
from aws_ssh_utils.terminal import Terminal


def feed_lines(terminal: Terminal, count: int):
    terminal.stream.feed(''.join(f'line {n}\r\n' for n in range(count)).encode())


def terminal_with(scrollback: int, channel: mock.Mock | None = None) -> Terminal:
    """A 20x5 terminal."""
    terminal = Terminal(mock.Mock(), channel or mock.Mock(), scrollback=scrollback)
    terminal.vt.resize(5, 20)
    return terminal


def test_lines_scrolled_off_the_top_are_kept_up_to_the_limit():
    terminal = terminal_with(scrollback=10)
    feed_lines(terminal, 30)

    history = [''.join(c.data for c in row).rstrip() for row in terminal.vt.history]
    assert history == [f'line {n}' for n in range(16, 26)]
    assert terminal.vt.display[:4] == [f'line {n}'.ljust(20) for n in range(26, 30)]


def test_clear_scrollback_empties_history():
    """`clear` sends CSI 3 J."""
    terminal = terminal_with(scrollback=10)
    feed_lines(terminal, 10)
    terminal.stream.feed(b'\x1b[3J')
    assert not terminal.vt.history


def test_no_scrollback_keeps_nothing():
    terminal = terminal_with(scrollback=0)
    feed_lines(terminal, 100)
    assert not terminal.vt.history


def test_scrolled_view_renders_history_then_screen():
    terminal = terminal_with(scrollback=100)
    feed_lines(terminal, 30)  # history: line 0..25, screen: line 26..29 and the prompt row
    terminal.scrolled = 2

    with mock.patch.object(type(terminal), 'has_focus', new_callable=mock.PropertyMock, return_value=False):
        rendered = [terminal.render_line(y).text.rstrip() for y in range(5)]

    assert rendered == ['line 24', 'line 25', 'line 26', 'line 27', 'line 28']


def test_typing_jumps_back_to_the_live_screen():
    channel = mock.Mock()
    channel.send.side_effect = len
    terminal = terminal_with(scrollback=100, channel=channel)
    feed_lines(terminal, 50)
    with mock.patch.object(terminal, 'refresh'):
        terminal.scroll_history(10)
        assert terminal.scrolled == 10
        terminal.send('x')
    assert terminal.scrolled == 0
    channel.send.assert_called_once_with(b'x')


@pytest.mark.parametrize(
    ('settings', 'profile_id', 'expected'),
    [
        pytest.param({'profiles': {'defaults': {'historySize': 5000}, 'list': []}}, '', 5000, id='defaults'),
        pytest.param(
            {'profiles': {'defaults': {'historySize': 5000}, 'list': [{'guid': '{ABC}', 'historySize': 20000}]}},
            '{abc}',
            20000,
            id='current-profile',
        ),
        pytest.param({'profiles': [{'guid': '{abc}'}]}, '{abc}', 9001, id='old-format-uses-wt-default'),
    ],
)
def test_windows_terminal_history_size(tmp_path, monkeypatch, settings, profile_id, expected):
    settings_file = tmp_path / cli_utils.WINDOWS_TERMINAL_SETTINGS[0]
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps(settings))
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.setenv('WT_SESSION', 'x')
    monkeypatch.setenv('WT_PROFILE_ID', profile_id)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))

    assert detect_scrollback() == expected


def test_tmux_history_limit_wins(monkeypatch):
    monkeypatch.setenv('TMUX', 'tmux-socket,1,0')
    monkeypatch.setenv('WT_SESSION', 'x')
    with mock.patch.object(cli_utils.subprocess, 'run') as run:
        run.return_value.stdout = '50000\n'
        assert detect_scrollback() == 50000


def test_unknown_terminal_falls_back(monkeypatch):
    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.delenv('WT_SESSION', raising=False)
    assert detect_scrollback() == cli_utils.DEFAULT_SCROLLBACK
