"""Keep diagnostics available without drawing over the full-screen terminal."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from loguru import logger
from textual.app import App
from typing_extensions import override

_terminal_active = False


def write_console(message: str) -> None:
    # Worker threads don't inherit Textual's active-app context. Suppress the
    # console process-wide while the UI runs; file logging remains enabled.
    if not _terminal_active:
        sys.stdout.write(message)


@contextmanager
def terminal_logging() -> Iterator[None]:
    global _terminal_active
    previous = _terminal_active
    _terminal_active = True
    try:
        yield
    finally:
        _terminal_active = previous


class LoggedApp(App[None]):
    @override
    def _handle_exception(self, error: Exception) -> None:
        # Textual handles UI errors internally instead of raising from run().
        logger.opt(exception=error).error('Unhandled {} error', type(self).__name__)
        super()._handle_exception(error)
