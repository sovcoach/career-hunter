"""Importing the resume text into the data folder, and loading it back for scoring.

All text here is made up.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from careerhunter import cli, db, resume
from tests.conftest import FakeProbe
from tests.test_resume import document_xml, make_docx, make_pdf

_TEXT = "Avery Example\nOperations coordinator, Example Widgets Ltd."
_OTHER = "Avery Example\nProject assistant, Sample Logistics Co."
_STAMP = "2026-11-04T12:00:00Z"
_CONFIG = {"search": {"resume": resume.CONFIG_VALUE}}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    return data


@pytest.fixture
def conn(root: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(root / "search.db")
    db.migrate(c, "search")
    yield c
    c.close()


def do_import(root: Path, conn: sqlite3.Connection, text: str = _TEXT) -> resume.Imported:
    return resume.import_text(root, conn, text, now=lambda: _STAMP)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def folder_names(root: Path) -> set[str]:
    return {p.name for p in (root / resume.RESUME_DIR).iterdir()}


def imports(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute("SELECT sha256, imported_at FROM resume_import")]


# --- a successful import --------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077])
def test_import_writes_private_files_whatever_the_umask(
    root: Path, conn: sqlite3.Connection, umask: int
) -> None:
    old = os.umask(umask)
    try:
        imported = do_import(root, conn)
    finally:
        os.umask(old)
    folder = root / resume.RESUME_DIR
    assert mode(folder) == 0o700
    assert mode(folder / resume.CURRENT) == 0o600
    assert mode(folder / resume.LOCK) == 0o600
    assert (folder / resume.CURRENT).read_text() == _TEXT
    assert imported == resume.Imported(sha(_TEXT), len(_TEXT))
    assert imports(conn) == [(sha(_TEXT), _STAMP)]


def test_a_loose_existing_folder_is_tightened(root: Path, conn: sqlite3.Connection) -> None:
    (root / resume.RESUME_DIR).mkdir(mode=0o755)
    os.chmod(root / resume.RESUME_DIR, 0o755)  # noqa: S103 - deliberately too loose
    do_import(root, conn)
    assert mode(root / resume.RESUME_DIR) == 0o700


def test_a_second_import_replaces_the_first_and_leaves_no_previous_file(
    root: Path, conn: sqlite3.Connection
) -> None:
    do_import(root, conn)
    do_import(root, conn, _OTHER)
    assert (root / resume.RESUME_DIR / resume.CURRENT).read_text() == _OTHER
    assert folder_names(root) == {resume.CURRENT, resume.LOCK}
    assert [s for s, _ in imports(conn)] == [sha(_TEXT), sha(_OTHER)]
    loaded = resume.load(root, conn, _CONFIG)
    assert loaded is not None and loaded.text == _OTHER and loaded.sha256 == sha(_OTHER)


def test_the_rename_happens_inside_the_database_transaction(
    root: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        seen.append(conn.in_transaction)
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    do_import(root, conn)
    assert seen == [True]


def test_the_text_is_never_stored_in_search_db(root: Path, conn: sqlite3.Connection) -> None:
    do_import(root, conn)
    # iterdump writes every row of every table; a BLOB appears as hex.
    dump = "\n".join(conn.iterdump())
    assert 'INSERT INTO "resume_import"' in dump
    for needle in ("Example Widgets", b"Example Widgets".hex()):
        assert needle not in dump


# --- refusing to write ---------------------------------------------------------------------


def test_a_resume_folder_that_is_a_link_is_refused(
    root: Path, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / resume.RESUME_DIR).symlink_to(elsewhere)
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn)
    assert caught.value.reason == "folder"
    assert list(elsewhere.iterdir()) == []
    assert imports(conn) == []


def test_a_current_file_that_is_a_link_is_refused(
    root: Path, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("untouched")
    folder = root / resume.RESUME_DIR
    folder.mkdir(mode=0o700)
    (folder / resume.CURRENT).symlink_to(target)
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn)
    assert caught.value.reason == "folder"
    assert target.read_text() == "untouched"
    assert imports(conn) == []
    assert not list(folder.glob(f"{resume.TMP_PREFIX}*"))


def test_the_temporary_file_is_created_exclusively(
    root: Path, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link planted at the temporary name is not followed or overwritten."""
    folder = root / resume.RESUME_DIR
    folder.mkdir(mode=0o700)
    target = tmp_path / "target.txt"
    target.write_text("untouched")
    monkeypatch.setattr(resume.secrets, "token_hex", lambda _n: "fixedname")
    # Planted after the leftover sweep, as a racing process would.
    real_remove = resume._remove_leftovers

    def remove_then_plant(where: Path) -> None:
        real_remove(where)
        (where / f"{resume.TMP_PREFIX}fixedname{resume.TMP_SUFFIX}").symlink_to(target)

    monkeypatch.setattr(resume, "_remove_leftovers", remove_then_plant)
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn)
    assert caught.value.reason == "write"
    assert target.read_text() == "untouched"
    assert imports(conn) == []


def test_a_failed_insert_changes_nothing(
    root: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    do_import(root, conn)
    conn.execute(
        "CREATE TRIGGER no_more BEFORE INSERT ON resume_import "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn, _OTHER)
    assert caught.value.reason == "write"
    assert (root / resume.RESUME_DIR / resume.CURRENT).read_text() == _TEXT
    assert folder_names(root) == {resume.CURRENT, resume.LOCK}
    assert resume.load(root, conn, _CONFIG) == resume.Resume(_TEXT, sha(_TEXT))


# --- a failed COMMIT after the rename --------------------------------------------------------


@contextmanager
def commit_fails(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """db.transaction whose COMMIT fails: the body runs, then everything rolls back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")
    raise sqlite3.OperationalError("disk I/O error")


def test_commit_failure_after_the_rename_restores_the_previous_file(
    root: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    do_import(root, conn)
    monkeypatch.setattr(resume.db, "transaction", commit_fails)
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn, _OTHER)
    assert caught.value.reason == "incomplete"
    assert caught.value.__cause__ is None
    assert (root / resume.RESUME_DIR / resume.CURRENT).read_text() == _TEXT
    assert folder_names(root) == {resume.CURRENT, resume.LOCK}
    assert [s for s, _ in imports(conn)] == [sha(_TEXT)]
    assert resume.load(root, conn, _CONFIG) == resume.Resume(_TEXT, sha(_TEXT))


def test_commit_failure_on_a_first_import_removes_the_new_file(
    root: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resume.db, "transaction", commit_fails)
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn)
    assert caught.value.reason == "incomplete"
    assert folder_names(root) == {resume.LOCK}
    assert imports(conn) == []
    with pytest.raises(resume.ResumeError) as loading:
        resume.load(root, conn, _CONFIG)
    assert loading.value.reason == "not_imported"


def test_when_the_restore_fails_too_the_mismatch_is_refused(
    root: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    do_import(root, conn)
    monkeypatch.setattr(resume.db, "transaction", commit_fails)
    real = os.replace
    calls: list[str] = []

    def replace_once(src: Any, dst: Any) -> None:
        calls.append(Path(src).name)
        if len(calls) > 1:
            raise OSError("restore failed")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace_once)
    with pytest.raises(resume.ResumeError) as caught:
        do_import(root, conn, _OTHER)
    assert caught.value.reason == "incomplete"
    assert calls[1] == resume.PREVIOUS  # the restore was attempted
    assert (root / resume.RESUME_DIR / resume.CURRENT).read_text() == _OTHER
    with pytest.raises(resume.ResumeError) as loading:
        resume.load(root, conn, _CONFIG)
    assert loading.value.reason == "mismatch"


# --- .previous.txt and temporary files left over from a crash --------------------------------


def test_a_leftover_previous_file_is_removed_when_no_swap_is_running(
    root: Path, conn: sqlite3.Connection
) -> None:
    do_import(root, conn)
    folder = root / resume.RESUME_DIR
    (folder / resume.PREVIOUS).write_text("left over")
    (folder / f"{resume.TMP_PREFIX}dead{resume.TMP_SUFFIX}").write_text("left over")
    do_import(root, conn, _OTHER)
    assert folder_names(root) == {resume.CURRENT, resume.LOCK}
    assert (folder / resume.CURRENT).read_text() == _OTHER


def test_a_swap_in_progress_is_refused_and_its_previous_file_kept(
    root: Path, conn: sqlite3.Connection
) -> None:
    do_import(root, conn)
    folder = root / resume.RESUME_DIR
    (folder / resume.PREVIOUS).write_text("another import's previous file")
    holder = os.open(folder / resume.LOCK, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(resume.ResumeError) as caught:
            do_import(root, conn, _OTHER)
    finally:
        os.close(holder)
    assert caught.value.reason == "import_running"
    assert (folder / resume.PREVIOUS).read_text() == "another import's previous file"
    assert (folder / resume.CURRENT).read_text() == _TEXT
    assert [s for s, _ in imports(conn)] == [sha(_TEXT)]


# --- load ------------------------------------------------------------------------------------


@pytest.mark.parametrize("config", [{}, {"search": {}}, {"search": {"resume": None}}])
def test_no_resume_configured_loads_nothing(
    root: Path, conn: sqlite3.Connection, config: dict[str, Any]
) -> None:
    assert resume.load(root, conn, config) is None


def _load_reason(root: Path, conn: sqlite3.Connection, config: dict[str, Any]) -> str:
    with pytest.raises(resume.ResumeError) as caught:
        resume.load(root, conn, config)
    assert caught.value.__cause__ is None
    return caught.value.reason


def test_another_path_in_search_resume_is_refused(root: Path, conn: sqlite3.Connection) -> None:
    do_import(root, conn)
    assert _load_reason(root, conn, {"search": {"resume": "resume.txt"}}) == "config"


def test_a_configured_resume_with_no_import_is_refused(
    root: Path, conn: sqlite3.Connection
) -> None:
    assert _load_reason(root, conn, _CONFIG) == "not_imported"


def test_a_missing_file_is_refused(root: Path, conn: sqlite3.Connection) -> None:
    do_import(root, conn)
    (root / resume.RESUME_DIR / resume.CURRENT).unlink()
    assert _load_reason(root, conn, _CONFIG) == "missing"


def test_a_loose_file_is_refused(root: Path, conn: sqlite3.Connection) -> None:
    do_import(root, conn)
    os.chmod(root / resume.RESUME_DIR / resume.CURRENT, 0o644)
    assert _load_reason(root, conn, _CONFIG) == "loose"


def test_an_edited_file_is_refused(root: Path, conn: sqlite3.Connection) -> None:
    do_import(root, conn)
    (root / resume.RESUME_DIR / resume.CURRENT).write_text(_TEXT + " edited")
    assert _load_reason(root, conn, _CONFIG) == "mismatch"


def test_a_linked_file_is_refused(root: Path, conn: sqlite3.Connection, tmp_path: Path) -> None:
    do_import(root, conn)
    current = root / resume.RESUME_DIR / resume.CURRENT
    copy = tmp_path / "copy.txt"
    copy.write_text(_TEXT)
    current.unlink()
    current.symlink_to(copy)
    assert _load_reason(root, conn, _CONFIG) == "mismatch"


def test_repr_never_shows_the_text(root: Path, conn: sqlite3.Connection) -> None:
    do_import(root, conn)
    loaded = resume.load(root, conn, _CONFIG)
    assert loaded is not None
    assert "Avery" not in repr(loaded) and "Avery" not in str(loaded)
    assert sha(_TEXT)[:12] in repr(loaded)


# --- the command ----------------------------------------------------------------------------


@pytest.fixture
def inited(data_env: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    capsys.readouterr()
    return data_env


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_resume_import_command(
    inited: Path,
    probe: FakeProbe,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    source = tmp_path / f"resume-file.{kind}"
    line = "Avery Example, operations coordinator"
    if kind == "pdf":
        source.write_bytes(make_pdf([line]))
    else:
        source.write_bytes(make_docx(document_xml(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>")))
    assert cli.main(["resume", "import", str(source)], probe=probe) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert out == (
        f"imported resume: {len(line)} characters\n"
        f"set search.resume: {resume.CONFIG_VALUE} in config.yaml to use it\n"
    )
    assert err == ""
    assert (inited / resume.RESUME_DIR / resume.CURRENT).read_text() == line
    assert str(source) not in out and "Avery" not in out


def test_a_refused_file_stores_nothing(
    inited: Path, probe: FakeProbe, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Avery Example plain text")
    assert cli.main(["resume", "import", str(source)], probe=probe) == cli.EXIT_ERROR
    out, err = capsys.readouterr()
    assert err == "error: the file is not a PDF or DOCX; nothing was imported\n"
    assert out == ""
    assert not (inited / resume.RESUME_DIR).exists()
    c = db.connect(inited / "search.db")
    try:
        assert c.execute("SELECT count(*) FROM resume_import").fetchone()[0] == 0
    finally:
        c.close()
