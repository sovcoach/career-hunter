from __future__ import annotations

import os
from pathlib import Path

import pytest

from careerhunter import storage_guard
from careerhunter.storage_guard import ENV_VAR, StorageGuardError, check, create
from tests.conftest import FakeProbe


def env_for(path: Path | str) -> dict[str, str]:
    return {ENV_VAR: str(path)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path.resolve() / "repo"
    r.mkdir()
    return r


def guard(data: Path, probe: FakeProbe, repo: Path, **kw: bool) -> storage_guard.DataDir:
    return check(env_for(data), probe=probe, repo=repo, require_exists=kw.get("exists", False))


def test_unset_env_refuses(probe: FakeProbe, repo: Path) -> None:
    with pytest.raises(StorageGuardError, match="not set"):
        check({}, probe=probe, repo=repo)
    with pytest.raises(StorageGuardError, match="not set"):
        check({ENV_VAR: "   "}, probe=probe, repo=repo)


def test_relative_path_refuses(probe: FakeProbe, repo: Path) -> None:
    with pytest.raises(StorageGuardError, match="absolute"):
        check(env_for("CareerHunterData"), probe=probe, repo=repo)


def test_data_inside_repo_refuses(repo: Path) -> None:
    probe = FakeProbe(repo.parent)
    with pytest.raises(StorageGuardError, match="inside the code repository"):
        guard(repo / "data", probe, repo)


def test_repo_inside_data_refuses(fake_volume: Path) -> None:
    data = fake_volume / "CareerHunterData"
    inner_repo = data / "career-hunter"
    with pytest.raises(StorageGuardError, match="inside the data directory"):
        guard(data, FakeProbe(fake_volume), inner_repo)


def test_symlink_into_repo_refuses(fake_volume: Path, repo: Path) -> None:
    (repo / "data").mkdir()
    link = fake_volume / "CareerHunterData"
    link.symlink_to(repo / "data")
    with pytest.raises(StorageGuardError, match="inside the code repository"):
        guard(link, FakeProbe(fake_volume), repo)


def test_any_git_work_tree_refuses(fake_volume: Path, repo: Path) -> None:
    probe = FakeProbe(fake_volume, git_top=fake_volume)
    with pytest.raises(StorageGuardError, match="git work tree"):
        guard(fake_volume / "CareerHunterData", probe, repo)


def test_non_macos_refuses(fake_volume: Path, repo: Path) -> None:
    with pytest.raises(StorageGuardError, match="macOS"):
        guard(fake_volume / "d", FakeProbe(fake_volume, macos=False), repo)


def test_unmounted_volume_refuses_and_names_boot(fake_volume: Path, repo: Path) -> None:
    probe = FakeProbe(fake_volume, mounted=False)
    with pytest.raises(StorageGuardError, match="boot volume"):
        guard(fake_volume / "CareerHunterData", probe, repo)


def test_same_device_as_boot_refuses(fake_volume: Path, repo: Path) -> None:
    probe = FakeProbe(fake_volume, same_device=True)
    with pytest.raises(StorageGuardError, match="same device"):
        guard(fake_volume / "d", probe, repo)


@pytest.mark.parametrize(
    ("volume", "message"),
    [
        ({"MountPoint": "/Volumes/Other"}, "does not report"),
        ({"APFSContainerReference": "disk3"}, "same APFS container"),
        ({"Internal": True}, "internal disk"),
        ({"Internal": None}, "internal disk"),
        ({"FilesystemType": "exfat"}, "not APFS"),
        ({"Encryption": False}, "NOT encrypted"),
        ({"Encryption": None}, "NOT encrypted"),
        ({"Locked": True}, "locked"),
    ],
)
def test_volume_properties_fail_closed(
    fake_volume: Path, repo: Path, volume: dict[str, object], message: str
) -> None:
    probe = FakeProbe(fake_volume, volume=volume)
    with pytest.raises(StorageGuardError, match=message):
        guard(fake_volume / "d", probe, repo)


def test_unencrypted_message_explains_fix(fake_volume: Path, repo: Path) -> None:
    probe = FakeProbe(fake_volume, volume={"Encryption": False})
    with pytest.raises(StorageGuardError) as exc:
        guard(fake_volume / "d", probe, repo)
    assert "diskutil apfs encryptVolume disk9s1" in str(exc.value)


def test_unreadable_diskutil_refuses(fake_volume: Path, repo: Path) -> None:
    probe = FakeProbe(fake_volume)
    probe.diskutil_info = lambda target: None  # type: ignore[method-assign]
    with pytest.raises(StorageGuardError, match="Could not read"):
        guard(fake_volume / "d", probe, repo)


def test_happy_path_and_missing_dir(fake_volume: Path, repo: Path, probe: FakeProbe) -> None:
    data = fake_volume / "CareerHunterData"
    result = guard(data, probe, repo)
    assert result.root == data
    assert result.mount_point == fake_volume
    with pytest.raises(StorageGuardError, match="careerhunter init"):
        guard(data, probe, repo, exists=True)


def test_create_makes_private_dirs(fake_volume: Path, repo: Path, probe: FakeProbe) -> None:
    datadir = guard(fake_volume / "CareerHunterData", probe, repo)
    created = create(datadir)
    assert datadir.root in created
    assert os.stat(datadir.root).st_mode & 0o777 == 0o700
    for sub in storage_guard.SUBDIRS:
        assert (datadir.root / sub).is_dir()
    assert guard(datadir.root, probe, repo, exists=True).root == datadir.root
    assert create(datadir) == []  # idempotent


def test_create_never_builds_missing_parents(
    fake_volume: Path, repo: Path, probe: FakeProbe
) -> None:
    datadir = guard(fake_volume / "missing" / "CareerHunterData", probe, repo)
    with pytest.raises(StorageGuardError, match="refusing to create"):
        create(datadir)
    assert not (fake_volume / "missing").exists()


def test_loose_permissions_refuse(fake_volume: Path, repo: Path, probe: FakeProbe) -> None:
    data = fake_volume / "CareerHunterData"
    data.mkdir(mode=0o755)
    os.chmod(data, 0o755)  # noqa: S103 - deliberately too loose
    with pytest.raises(StorageGuardError, match="chmod 700"):
        guard(data, probe, repo, exists=True)


def test_find_mount_point_walks_to_nearest_mount(fake_volume: Path, probe: FakeProbe) -> None:
    assert storage_guard.find_mount_point(fake_volume / "a" / "b", probe) == fake_volume


def test_repo_root_finds_this_checkout() -> None:
    root = storage_guard.repo_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
