"""A run's log lines, written to a private file as they happen.

`careerhunter watch` and `careerhunter discover` run under launchd, which discards
stderr. So each line goes to `logs/<name>.log` in the data folder the moment it is
produced, then to stderr, and a crash later in the run cannot lose it.

The file is opened only after the storage guard has passed. It is opened without
following a symlink, must be a regular file, and is kept at mode 0600 in a 0700
folder. Each line is written whole, retrying a short write. A file that cannot be
opened or written is a RunLogError, and the run stops: a run that cannot leave a trace
should not go on. A failed write to stderr, such as a closed terminal, is ignored.

Rotation (#42). When a run opens a log that has reached MAX_BYTES, the standard
library's `RotatingFileHandler` rolls it over first: `watch.log` becomes `watch.log.1`,
`.1` becomes `.2`, and so on, keeping BACKUPS old files in `logs/`; the oldest is
removed. A rename keeps the file's mode, every rotated file is set to 0600 again, and
the new file is created 0600 like any other, so no umask can loosen one. A log can pass
MAX_BYTES by one run's lines before it is rolled over. A rotation that fails is a
RunLogError, like a file that can't be opened.
"""

from __future__ import annotations

import contextlib
import io
import logging.handlers
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TextIO

Writer = Callable[[int, memoryview], int]

MAX_BYTES = 5_000_000
BACKUPS = 5


class _PrivateRotation(logging.handlers.RotatingFileHandler):
    """The standard library's rotation, used only to roll a log over. It never opens a
    stream of its own (`delay`); were it asked to, the file would be created 0600 and
    without following a symlink."""

    def __init__(self, path: Path, backups: int) -> None:
        super().__init__(path, maxBytes=0, backupCount=backups, delay=True)

    def _open(self) -> io.TextIOWrapper:
        fd = os.open(
            self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def rotate_if_full(path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> bool:
    """Roll `path` over when it is a regular file of at least `max_bytes`. True if it was.

    A symlink is left alone (the open after this refuses it anyway).
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size < max_bytes:
        return False
    handler = _PrivateRotation(path, backups)
    try:
        handler.doRollover()
    finally:
        handler.close()
    for index in range(1, backups + 1):
        rotated = path.with_name(f"{path.name}.{index}")
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISREG(rotated.lstat().st_mode):
                os.chmod(rotated, 0o600)
    return True


class RunLogError(Exception):
    """The run log could not be opened or written. The message names no path."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"run log not writable: {reason}")


class RunLog:
    """One run's log file, opened for appending."""

    def __init__(
        self,
        path: Path,
        stream: TextIO | None = None,
        *,
        writer: Writer = os.write,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._writer = writer
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            rotate_if_full(path, max_bytes)
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RunLogError(type(exc).__name__) from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RunLogError("not a regular file")
            os.fchmod(fd, 0o600)
        except OSError as exc:
            os.close(fd)
            raise RunLogError(type(exc).__name__) from None
        except RunLogError:
            os.close(fd)
            raise
        self._fd = fd

    def append(self, line: str) -> None:
        """One line: to the file first, then to the stream."""
        self._write(line + "\n")
        with contextlib.suppress(OSError, ValueError):  # ValueError: a closed stream
            self._stream.write(line + "\n")
            self._stream.flush()

    def note(self, text: str) -> None:
        """Text for the file only, such as an abort line or a traceback."""
        self._write(text if text.endswith("\n") else text + "\n")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> RunLog:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _write(self, text: str) -> None:
        pending = memoryview(text.encode("utf-8", "backslashreplace"))
        while pending:
            try:
                written = self._writer(self._fd, pending)
            except OSError as exc:
                raise RunLogError(type(exc).__name__) from None
            if written <= 0:
                raise RunLogError("nothing written")
            pending = pending[written:]
