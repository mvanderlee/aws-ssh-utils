"""Terminal channels for native, keyless Session Manager shells."""

import os
import queue
import re
import threading
import time
from typing import Protocol, cast


class ShellChannel(Protocol):
    def recv(self, nbytes: int, /) -> bytes: ...
    def send(self, data: bytes, /) -> int: ...
    def resize_pty(self, width: int, height: int) -> None: ...


class ShellClient(Protocol):
    def close(self) -> None: ...


class Pty(Protocol):
    def read(self, size: int) -> str: ...
    def write(self, data: str) -> int: ...
    def setwinsize(self, rows: int, cols: int) -> None: ...
    def close(self, force: bool = False) -> None: ...


def spawn_pty(command: list[str]) -> Pty:
    """SSM needs a real terminal for raw input, control keys, and window sizes."""
    env = {**os.environ, 'TERM': 'xterm-256color', 'AWS_PAGER': '', 'AWS_CLI_AUTO_PROMPT': 'off'}
    if os.name == 'nt':
        from winpty import PtyProcess

        return cast(Pty, PtyProcess.spawn(command, env=env, dimensions=(24, 80)))

    from ptyprocess import PtyProcessUnicode

    return cast(Pty, PtyProcessUnicode.spawn(command, env=env, dimensions=(24, 80), echo=False))


class SSMSession:
    """Adapt an AWS CLI pseudo-terminal to the terminal widget's channel interface."""

    def __init__(self, process: Pty):
        self.process = process
        self.closed = False
        self._output: queue.Queue[bytes | Exception | None] = queue.Queue()
        self._pending = b''
        self._eof = False
        threading.Thread(target=self._read, daemon=True).start()

    @classmethod
    def open(cls, command: list[str], timeout: float = 30) -> 'SSMSession':
        session = cls(spawn_pty(command))
        try:
            session._wait_started(timeout)
        except BaseException:
            session.close()
            raise
        return session

    def _read(self):
        try:
            while not self.closed:
                data = self.process.read(65536)
                if data:
                    self._output.put(data.encode('utf-8'))
        except EOFError:
            pass
        except Exception as e:
            if not self.closed:
                self._output.put(e)
        finally:
            self._output.put(None)

    def _wait_started(self, timeout: float):
        """Keep startup output for the terminal, but report CLI failures to the fallback list."""
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            while True:
                data = self._output.get(timeout=max(0, deadline - time.monotonic()))
                if isinstance(data, Exception):
                    raise OSError(str(data)) from data
                if data is None:
                    raise OSError(output.decode('utf-8', errors='replace').strip() or 'SSM exited before starting a session')
                output.extend(data)
                # ConPTY adds terminal escape sequences to console output on Windows.
                text = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', output)
                if re.search(rb'Starting session with SessionId:\s*\S+', text):
                    self._pending = bytes(output)
                    return
        except queue.Empty:
            detail = output.decode('utf-8', errors='replace').strip()
            raise TimeoutError(f'Timed out starting SSM session{": " + detail if detail else ""}') from None

    def recv(self, nbytes: int) -> bytes:
        if nbytes <= 0:
            return b''
        while not self._pending and not self._eof:
            data = self._output.get()
            if isinstance(data, Exception):
                raise OSError(str(data)) from data
            if data is None:
                self._eof = True
            else:
                self._pending = data
        data, self._pending = self._pending[:nbytes], self._pending[nbytes:]
        return data

    def send(self, data: bytes) -> int:
        try:
            self.process.write(data.decode('utf-8'))
        except EOFError as e:
            raise OSError('SSM session is closed') from e
        return len(data)

    def resize_pty(self, width: int, height: int):
        if not self.closed:
            self.process.setwinsize(height, width)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.process.close(force=True)
            finally:
                self._output.put(None)
