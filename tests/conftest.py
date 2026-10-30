"""Shared fixtures. Everything here is synthetic; nothing touches real data or disks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend

from careerhunter import db, storage_guard
from careerhunter.platform_checks import SystemProbe

FIXTURES = Path(__file__).parent / "fixtures"


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
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


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


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[InMemoryKeyring]:
    """Every test gets an in-memory keyring, so no test can ever touch the real Keychain."""
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)
