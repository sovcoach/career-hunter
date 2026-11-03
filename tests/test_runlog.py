"""The run log: lines on disk as they happen, private, and never through a symlink."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from careerhunter.runlog import RunLog, RunLogError


def test_the_file_and_folder_are_private(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "watch.log"
    with RunLog(path, io.StringIO()) as log:
        log.append("watch: one")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_an_existing_open_file_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "watch.log"
    path.write_text("old\n")
    path.chmod(0o644)
    with RunLog(path, io.StringIO()) as log:
        log.append("new")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == "old\nnew\n"  # appended, never truncated


def test_append_goes_to_the_file_and_the_stream_and_note_to_the_file_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watch.log"
    stream = io.StringIO()
    with RunLog(path, stream) as log:
        log.append("watch: one")
        log.note("Traceback (most recent call last):\n  synthetic")
        log.append("watch: two")
    assert path.read_text() == (
        "watch: one\nTraceback (most recent call last):\n  synthetic\nwatch: two\n"
    )
    assert stream.getvalue() == "watch: one\nwatch: two\n"


def test_the_file_is_written_before_the_stream(tmp_path: Path) -> None:
    path = tmp_path / "watch.log"
    seen: list[str] = []

    class Peek(io.StringIO):
        def write(self, text: str) -> int:
            seen.append(path.read_text())  # what is on disk when the stream is written
            return super().write(text)

    with RunLog(path, Peek()) as log:
        log.append("watch: one")
    assert seen == ["watch: one\n"]


@pytest.mark.parametrize("error", [BrokenPipeError(), OSError("closed"), ValueError("closed")])
def test_a_failing_stream_is_ignored(tmp_path: Path, error: Exception) -> None:
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise error

    path = tmp_path / "watch.log"
    with RunLog(path, Broken()) as log:
        log.append("watch: one")
        log.append("watch: two")
    assert path.read_text() == "watch: one\nwatch: two\n"


def test_a_failing_file_write_raises(tmp_path: Path) -> None:
    def refuse(fd: int, data: memoryview) -> int:
        raise OSError("disk full")

    stream = io.StringIO()
    log = RunLog(tmp_path / "watch.log", stream, writer=refuse)
    with pytest.raises(RunLogError) as exc:
        log.append("watch: one")
    log.close()
    assert str(exc.value) == "run log not writable: OSError"
    assert stream.getvalue() == ""  # the stream comes after the file, so nothing went out


def test_a_short_write_is_retried_until_the_line_is_whole(tmp_path: Path) -> None:
    sizes: list[int] = []

    def half_then_rest(fd: int, data: memoryview) -> int:
        size = len(data) // 2 if not sizes else len(data)
        sizes.append(size)
        return os.write(fd, data[:size])

    path = tmp_path / "watch.log"
    with RunLog(path, io.StringIO(), writer=half_then_rest) as log:
        log.append("watch: a line long enough to split")
    assert path.read_text() == "watch: a line long enough to split\n"
    assert len(sizes) == 2


def test_a_zero_length_write_raises(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "watch.log", io.StringIO(), writer=lambda fd, data: 0)
    with pytest.raises(RunLogError, match="nothing written"):
        log.append("watch: one")
    log.close()


def test_a_symlink_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep me\n")
    target.chmod(0o644)
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    (logs / "watch.log").symlink_to(target)
    with pytest.raises(RunLogError) as exc:
        RunLog(logs / "watch.log", io.StringIO())
    assert str(tmp_path) not in str(exc.value)
    assert target.read_text() == "keep me\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_a_directory_in_place_of_the_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "watch.log").mkdir()
    with pytest.raises(RunLogError) as exc:
        RunLog(tmp_path / "watch.log", io.StringIO())
    assert str(tmp_path) not in str(exc.value)


def test_a_fifo_in_place_of_the_file_is_refused_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "watch.log")
    with pytest.raises(RunLogError):  # no reader: the non-blocking open fails at once
        RunLog(tmp_path / "watch.log", io.StringIO())


def test_a_fifo_with_a_reader_is_refused_after_opening(tmp_path: Path) -> None:
    path = tmp_path / "watch.log"
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(RunLogError, match="not a regular file"):
            RunLog(path, io.StringIO())
    finally:
        os.close(reader)


def test_a_failed_permission_change_closes_the_file_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careerhunter import runlog

    opened: list[int] = []
    real_open = os.open

    def recording_open(path: object, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    def refuse(fd: int, mode: int) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(runlog.os, "open", recording_open)
    monkeypatch.setattr(runlog.os, "fchmod", refuse)
    path = tmp_path / "watch.log"
    with pytest.raises(RunLogError) as exc:
        RunLog(path, io.StringIO())
    assert str(exc.value) == "run log not writable: OSError"
    assert str(tmp_path) not in str(exc.value)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed
    assert path.read_bytes() == b""
