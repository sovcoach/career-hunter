"""A read-only copy of the databases, for investigations (`careerhunter db snapshot`).

One run copies search.db and every EI record file in the data folder into a new
folder under the destination, `careerhunter-snapshot-<UTC time>`. Each source is
opened read-only (`mode=ro`) and copied with SQLite's online backup API, so a copy is
consistent even while the watcher or the bot is writing, and the source is never
changed. Each copy is owner-only (0600, in a 0700 folder), uses a rollback journal so
it is one file, and must pass `PRAGMA integrity_check` or the run fails.

The destination must already exist, must not be inside the data folder or inside a
git work tree (a checkout of this repo, or any other), and must be on a volume that
diskutil reports as encrypted. Nothing is printed about where the sources live.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from careerhunter import db
from careerhunter.platform_checks import SystemProbe
from careerhunter.storage_guard import DataDir

FOLDER_PREFIX = "careerhunter-snapshot-"


class SnapshotError(Exception):
    """The snapshot was refused or a copy failed its check."""


@dataclass(frozen=True)
class Copied:
    name: str
    integrity: str


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _mount_of(path: Path, probe: SystemProbe) -> Path:
    for candidate in (path, *path.parents):
        if probe.ismount(candidate):
            return candidate
    return Path("/")


def check_destination(dest: Path, datadir: DataDir, probe: SystemProbe) -> Path:
    """The resolved destination, or SnapshotError saying why it is refused."""
    if not dest.is_dir():
        raise SnapshotError(f"{dest} is not an existing folder")
    target = dest.resolve()
    if _within(target, datadir.root.resolve()):
        raise SnapshotError("the destination is inside the data folder; choose another folder")
    if probe.git_toplevel(target) is not None:
        raise SnapshotError("the destination is inside a git work tree; choose another folder")
    mount = _mount_of(target, probe)
    info = probe.diskutil_info(str(mount))
    if info is None or info.get("MountPoint") != str(mount):
        raise SnapshotError(f"diskutil could not report on the volume at {mount}")
    if info.get("Encryption") is not True:
        raise SnapshotError(
            f"diskutil does not report the volume at {mount} as encrypted; "
            "choose a folder on an encrypted volume"
        )
    if info.get("Locked") is True:
        raise SnapshotError(f"the volume at {mount} is locked")
    return target


def sources(datadir: DataDir) -> list[Path]:
    """search.db, then each EI record file, by name."""
    found = [datadir.search_db_path] if datadir.search_db_path.is_file() else []
    return found + [p for p in datadir.ei_record_files() if p.is_file()]


def _copy(source: Path, target: Path) -> str:
    os.close(os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    src = db.connect_read_only(source)
    try:
        dst = sqlite3.connect(target, isolation_level=None)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")
            row = dst.execute("PRAGMA integrity_check").fetchone()
            result = str(row[0]) if row else "no result"
        finally:
            dst.close()
    finally:
        src.close()
    for companion in db.db_files(target)[1:]:
        companion.unlink(missing_ok=True)
    os.chmod(target, 0o600)
    return result


def snapshot(
    dest: Path, datadir: DataDir, probe: SystemProbe, now: dt.datetime
) -> tuple[Path, list[Copied]]:
    """Copy every database into a new folder under `dest`. Raises SnapshotError."""
    target = check_destination(dest, datadir, probe)
    files = sources(datadir)
    if not files:
        raise SnapshotError("the data folder has no databases to copy")
    folder = target / f"{FOLDER_PREFIX}{now.astimezone(dt.UTC):%Y%m%dT%H%M%SZ}"
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SnapshotError(f"{folder.name} already exists; try again in a second") from exc
    os.chmod(folder, 0o700)
    copied = [Copied(source.name, _copy(source, folder / source.name)) for source in files]
    bad = [c.name for c in copied if c.integrity != "ok"]
    if bad:
        raise SnapshotError(f"integrity check failed for {', '.join(bad)} in {folder}")
    return folder, copied
