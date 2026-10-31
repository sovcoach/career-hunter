"""Shared fixtures. Everything here is synthetic; nothing touches real data or disks."""

from __future__ import annotations

import base64
import datetime as dt
import re
import sqlite3
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
import yaml
from keyring.backend import KeyringBackend

from careerhunter import claims, db, storage_guard
from careerhunter.claims import ActiveClaim, Claim, ClaimSettings
from careerhunter.platform_checks import SystemProbe
from careerhunter.storage_guard import DataDir

FIXTURES = Path(__file__).parent / "fixtures"

# Synthetic claims. The active one starts on a Wednesday, so a week can straddle it.
ACTIVE = Claim("2026-27", dt.date(2026, 8, 5))
EARLIER = Claim("2025-26", dt.date(2025, 6, 4), dt.date(2026, 6, 2))
ACTIVE_ONLY = ClaimSettings(active=ACTIVE, claims=(ACTIVE,))


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


class FakeProbe(SystemProbe):
    """Pretends `mount` is an external, encrypted APFS volume (unless told otherwise)."""

    def __init__(
        self,
        mount: Path,
        *,
        volume: dict[str, Any] | None = None,
        mounted: bool = True,
        macos: bool = True,
        filevault: bool | None = True,
        git_top: Path | None = None,
        same_device: bool = False,
    ) -> None:
        self.mount = mount
        self.mounted = mounted
        self.macos = macos
        self.filevault = filevault
        self.git_top = git_top
        self.same_device = same_device
        self.volume: dict[str, Any] = {
            "MountPoint": str(mount),
            "DeviceIdentifier": "disk9s1",
            "APFSContainerReference": "disk9",
            "Internal": False,
            "FilesystemType": "apfs",
            "Encryption": True,
            "Locked": False,
        }
        self.volume.update(volume or {})
        self.boot = {"MountPoint": "/", "APFSContainerReference": "disk3", "Internal": True}

    def is_macos(self) -> bool:
        return self.macos

    def ismount(self, path: Path) -> bool:
        return path == Path("/") or (self.mounted and path == self.mount)

    def st_dev(self, path: Path) -> int | None:
        if self.mounted and not self.same_device and _within(path, self.mount):
            return 2
        return 1

    def diskutil_info(self, target: str) -> dict[str, Any] | None:
        if target == "/":
            return dict(self.boot)
        return dict(self.volume) if self.mounted and Path(target) == self.mount else None

    def filevault_on(self) -> bool | None:
        return self.filevault

    def git_toplevel(self, path: Path) -> Path | None:
        return self.git_top


@pytest.fixture
def fake_volume(tmp_path: Path) -> Path:
    mount = tmp_path.resolve() / "Volumes" / "FakeX10"
    mount.mkdir(parents=True)
    return mount


@pytest.fixture
def probe(fake_volume: Path) -> FakeProbe:
    return FakeProbe(fake_volume)


@pytest.fixture
def data_env(fake_volume: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = fake_volume / "CareerHunterData"
    monkeypatch.setenv(storage_guard.ENV_VAR, str(data))
    return data


@pytest.fixture
def datadir(tmp_path: Path) -> DataDir:
    """A data folder for tests that call the library directly, without the guard."""
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    (root / "exports").mkdir(mode=0o700)
    return DataDir(root=root, mount_point=tmp_path)


@pytest.fixture
def opener() -> Iterator[Any]:
    """Open databases in a test and have them all closed afterwards.

    `opener(path)` opens read-write, `opener(path, track)` also migrates, and
    `opener(path, read_only=True)` opens read-only.
    """
    opened: list[sqlite3.Connection] = []

    def open_db(
        path: Path, track: str | None = None, *, read_only: bool = False
    ) -> sqlite3.Connection:
        c = db.connect_read_only(path) if read_only else db.connect(path)
        opened.append(c)
        if track:
            db.migrate(c, track)
        return c

    yield open_db
    for c in opened:
        c.close()


@pytest.fixture
def search(datadir: DataDir) -> Iterator[sqlite3.Connection]:
    c = db.connect(datadir.search_db_path)
    db.migrate(c, "search")
    yield c
    c.close()


@pytest.fixture
def active(datadir: DataDir) -> Iterator[ActiveClaim]:
    a = claims.open_active(datadir, ACTIVE_ONLY)
    assert a is not None
    yield a
    a.conn.close()


@pytest.fixture
def ei(active: ActiveClaim) -> sqlite3.Connection:
    return active.conn


def pdf_text(path: Path) -> str:
    """The decompressed page content of a ReportLab PDF, enough to find words in it."""
    out = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", path.read_bytes(), re.S):
        data = raw.strip()
        try:
            if data.endswith(b"~>"):
                data = base64.a85decode(data, adobe=True)
            out.append(zlib.decompress(data).decode("latin-1"))
        except (ValueError, zlib.error):
            continue
    return "\n".join(out)


def claim_entry(claim: Claim) -> dict[str, Any]:
    entry: dict[str, Any] = {"label": claim.label, "start": claim.start}
    if claim.end:
        entry["end"] = claim.end
    return entry


def set_claims(data_dir: Path, active: str | None, *entries: Claim | dict[str, Any]) -> None:
    """Write an `ei` section into the data folder's config.yaml."""
    path = data_dir / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["ei"] = {
        "active_claim": active,
        "claims": [claim_entry(e) if isinstance(e, Claim) else e for e in entries],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


class InMemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        raise AssertionError("career-hunter never deletes secrets in Phase 1")


SYNTHETIC_GIT_NAME = "Synthetic Test"
SYNTHETIC_GIT_EMAIL = "test@example.invalid"


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git call in the tests sees the same config, on any machine.

    The developer's global and system git config are hidden, the identity is a fixed
    synthetic one, and user.useConfigOnly stops git guessing an identity from the
    username and hostname (which macOS can do and a CI runner can't).
    """
    for key, value in {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": SYNTHETIC_GIT_NAME,
        "GIT_AUTHOR_EMAIL": SYNTHETIC_GIT_EMAIL,
        "GIT_COMMITTER_NAME": SYNTHETIC_GIT_NAME,
        "GIT_COMMITTER_EMAIL": SYNTHETIC_GIT_EMAIL,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.useConfigOnly",
        "GIT_CONFIG_VALUE_0": "true",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[InMemoryKeyring]:
    """Every test gets an in-memory keyring, so no test can ever touch the real Keychain."""
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)
