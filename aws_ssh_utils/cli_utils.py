"""Shared helpers for the interactive (questionary based) commands."""

import ctypes
import functools
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from shutil import which
from typing import ParamSpec, TypeVar

import click
import paramiko
import questionary
from botocore.exceptions import ClientError
from loguru import logger
from rich.console import Console
from rich.markup import escape
from rich.status import Status

from aws_ssh_utils.commands.healthcheck import is_aws_ssm_installed, is_opkssh_installed
from aws_ssh_utils.connection import Environment, Target, connect, open_shell_channel
from aws_ssh_utils.terminal import ShellApp

P = ParamSpec('P')
R = TypeVar('R')

DEFAULT_SCROLLBACK = 1000
WINDOWS_TERMINAL_DEFAULT_SCROLLBACK = 9001
WINDOWS_TERMINAL_SETTINGS = (
    'Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json',
    'Packages/Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe/LocalState/settings.json',
    'Microsoft/Windows Terminal/settings.json',
)
"""Relative to %LOCALAPPDATA%: Store, Store preview, and unpackaged installs."""

console = Console(stderr=True)


class ShellError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message, exit_code)
        self.message: str = message
        self.exit_code: int = exit_code


def spinner(message: str = '') -> Status:
    return console.status(message, spinner='dots')


def handle_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Log ShellErrors and AWS errors, and exit, instead of printing a traceback."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except ShellError as e:
            logger.error(e.message)
            sys.exit(e.exit_code)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'ExpiredTokenException':
                logger.critical('Your AWS Token has expired. Please update and try again.')
            else:
                logger.error(e)
            sys.exit(1)

    return wrapper


def set_terminal_title(title: str = ''):
    if os.name == 'nt':
        ctypes.windll.kernel32.SetConsoleTitleW(title)  # pyright: ignore[reportAttributeAccessIssue]
    elif os.getenv('TMUX'):
        sys.stdout.write(f"\33k{title}\33")
        sys.stdout.flush()
    else:
        sys.stdout.write(f"\33]0;{title}\a")
        sys.stdout.flush()


def detect_environment(profile: str | None, region: str | None) -> Environment:
    return Environment(
        ssm_installed=is_aws_ssm_installed() and which('aws') is not None,
        opkssh_installed=is_opkssh_installed(),
        profile=profile,
        region=region,
    )


def detect_scrollback() -> int:
    """The scrollback of the terminal we run in, when it can be read (tmux, Windows Terminal)."""
    if os.getenv('TMUX'):
        try:
            result = subprocess.run(
                ['tmux', 'show-options', '-gv', 'history-limit'],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return int(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            logger.debug(f"Could not read tmux history-limit: {e}")
    if os.getenv('WT_SESSION'):
        return windows_terminal_history_size()
    return DEFAULT_SCROLLBACK


def windows_terminal_history_size() -> int:
    """`historySize` of the current Windows Terminal profile, falling back to the profile defaults."""
    local_app_data = Path(os.getenv('LOCALAPPDATA', ''))
    for settings_file in WINDOWS_TERMINAL_SETTINGS:
        try:
            settings = json.loads((local_app_data / settings_file).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        profiles = settings.get('profiles', {})
        # Older settings files have a plain list of profiles
        profile_list = profiles.get('list', []) if isinstance(profiles, dict) else profiles
        defaults = profiles.get('defaults', {}) if isinstance(profiles, dict) else {}
        profile_id = os.getenv('WT_PROFILE_ID', '').lower()
        profile = next((p for p in profile_list if p.get('guid', '').lower() == profile_id), {})
        return int(profile.get('historySize', defaults.get('historySize', WINDOWS_TERMINAL_DEFAULT_SCROLLBACK)))
    return WINDOWS_TERMINAL_DEFAULT_SCROLLBACK


scrollback_option = click.option(
    '--scrollback',
    type=click.IntRange(min=0),
    default=None,
    help="Lines of scrollback per shell. Defaults to your tmux or Windows Terminal setting, else 1000.",
)


def confirm_host_key(hostname: str, key: paramiko.PKey) -> bool:
    logger.warning(f"Unknown {key.get_name()} host key for {hostname}: {key.fingerprint}")
    return questionary.confirm("Continue and add host key?").unsafe_ask()


def open_shell(target: Target, env: Environment, title: str, scrollback: int | None, initial_input: bytes = b''):
    """Connect to the target, then show the shell full-screen until it closes.

    `scrollback` defaults to the terminal's own setting, see `detect_scrollback`.
    """
    result = connect(target, env, on_output=logger.info, on_waiting=lambda: None, confirm_host_key=confirm_host_key)
    if isinstance(result, list):
        for line in result:
            console.print(escape(line))
        raise ShellError(f'Failed to connect to {target.user}@{target.ip}')

    channel = open_shell_channel(result.client)
    if initial_input:
        channel.send(initial_input)

    set_terminal_title(title)
    # Logs, e.g. AWS SSM's stderr, would draw over the full-screen shell.
    logger.disable('aws_ssh_utils')
    try:
        ShellApp(result.via, result.client, channel, detect_scrollback() if scrollback is None else scrollback).run()
    finally:
        logger.enable('aws_ssh_utils')
        set_terminal_title()
