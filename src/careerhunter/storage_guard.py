"""Fail-closed checks on where personal data lives.

The app keeps everything personal (database, real config, job-ad snapshots, exports,
logs) in one data directory named by CAREERHUNTER_DATA_DIR. Before any command runs,
this module proves that directory is:

1. set explicitly, as an absolute path;
2. nowhere near a git repository (not inside this repo, not containing it, not inside
   any other git work tree);
3. on a mounted volume that is distinct from the boot volume, and not an internal disk;
4. on an encrypted, unlocked APFS volume.

If any check fails, or can't be verified, the app refuses to start. There is no flag
to skip these checks.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from careerhunter.platform_checks import SystemProbe

ENV_VAR = "CAREERHUNTER_DATA_DIR"
SUBDIRS = ("exports", "snapshots", "logs", "backups")
BOOT_PATHS = (Path("/"), Path("/System/Volumes/Data"))
EI_RECORD_PREFIX = "ei_record_"
# The shape of a claim label. It becomes part of a file name, so nothing else is allowed
# through; claims.py adds the rule that the two years are consecutive.
CLAIM_LABEL_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}$")


class StorageGuardError(Exception):
    """Raised when the data directory is not safe to use. The message is user-facing."""


@dataclass(frozen=True)
class DataDir:
    root: Path
    mount_point: Path

    @property
    def search_db_path(self) -> Path:
        return self.root / "search.db"

    @property
    def legacy_db_path(self) -> Path:
        """The single database of v0.1.0, which this version never opens for writing."""
        return self.root / "careerhunter.db"

    def ei_record_path(self, label: str) -> Path:
        if not CLAIM_LABEL_SHAPE.match(label):
            raise ValueError(f"{label!r} is not a claim label like 2026-27")
        return self.root / f"{EI_RECORD_PREFIX}{label}.db"

    def ei_record_files(self) -> list[Path]:
        return sorted(self.root.glob(f"{EI_RECORD_PREFIX}*.db"))

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def exclusions_path(self) -> Path:
        return self.root / "exclusions.yaml"

    @property
    def seed_path(self) -> Path:
        return self.root / "seed.yaml"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


def repo_root() -> Path | None:
    """The source checkout this package runs from, if it runs from one."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "careerhunter").is_dir():
            return parent
    return None


def _is_within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _nearest_existing(path: Path) -> Path:
    p = path
    while not p.exists():
        p = p.parent
    return p


def find_mount_point(path: Path, probe: SystemProbe) -> Path:
    p = _nearest_existing(path)
    while not probe.ismount(p):
        if p.parent == p:
            break
        p = p.parent
    return p


def _check_repo_separation(data: Path, probe: SystemProbe, repo: Path | None) -> None:
    if repo is not None:
        if _is_within(data, repo):
            raise StorageGuardError(
                f"{ENV_VAR} ({data}) is inside the code repository ({repo}). "
                "Personal data must live outside the repo so it can never be committed."
            )
        if _is_within(repo, data):
            raise StorageGuardError(
                f"The code repository ({repo}) is inside the data directory ({data}). "
                "Keep them completely separate."
            )
    toplevel = probe.git_toplevel(_nearest_existing(data))
    if toplevel is not None:
        raise StorageGuardError(
            f"{ENV_VAR} ({data}) is inside a git work tree ({toplevel}). "
            "Personal data must never sit inside any git repository."
        )


def _check_volume(data: Path, probe: SystemProbe) -> Path:
    if not probe.is_macos():
        raise StorageGuardError(
            "The storage guard can only verify volumes on macOS, so career-hunter refuses "
            "to run here."
        )
    mount = find_mount_point(data, probe)
    if mount == Path("/"):
        raise StorageGuardError(
            f"{ENV_VAR} ({data}) resolves to the boot volume. Either the external drive "
            "is not mounted, or the path points at the internal disk. Plug in and unlock "
            "the data drive, then try again."
        )
    mount_dev = probe.st_dev(mount)
    boot_devs = {probe.st_dev(p) for p in BOOT_PATHS} - {None}
    if mount_dev is None or mount_dev in boot_devs:
        raise StorageGuardError(
            f"{mount} is not a separate volume from the boot disk (same device). "
            "The data drive must be its own mounted volume."
        )

    info = probe.diskutil_info(str(mount))
    if info is None:
        raise StorageGuardError(f"Could not read volume details for {mount} via diskutil.")
    if info.get("MountPoint") != str(mount):
        raise StorageGuardError(
            f"diskutil does not report {mount} as a mounted volume "
            f"(it reports {info.get('MountPoint')!r})."
        )
    boot = probe.diskutil_info("/")
    if boot is None:
        raise StorageGuardError("Could not read boot volume details via diskutil.")
    if info.get("APFSContainerReference") and info.get("APFSContainerReference") == boot.get(
        "APFSContainerReference"
    ):
        raise StorageGuardError(
            f"{mount} lives in the same APFS container as the boot volume. "
            "The data must be on a separate physical drive."
        )
    if info.get("Internal") is not False:
        raise StorageGuardError(
            f"{mount} is an internal disk (or diskutil could not say). "
            "The data must live on the dedicated external drive."
        )
    if str(info.get("FilesystemType", "")).lower() != "apfs":
        raise StorageGuardError(
            f"{mount} is formatted {info.get('FilesystemType')!r}, not APFS. "
            "career-hunter requires an encrypted APFS volume."
        )
    if info.get("Encryption") is not True:
        raise StorageGuardError(
            f"{mount} is NOT encrypted. Your job-search record contains personal data, "
            "so career-hunter will not write it to an unencrypted drive.\n"
            "  Fix: in Finder, right-click the drive and choose Encrypt, or run\n"
            f"    diskutil apfs encryptVolume {info.get('DeviceIdentifier', '<disk>')} -user disk\n"
            "  Encryption runs in the background; the drive stays usable. See docs/SETUP.md."
        )
    if info.get("Locked") is True:
        raise StorageGuardError(f"{mount} is encrypted but locked. Unlock it, then try again.")
    return mount


def _check_permissions(data: Path) -> None:
    mode = stat.S_IMODE(data.stat().st_mode)
    if mode & 0o077:
        raise StorageGuardError(
            f"{data} is readable by other users (mode {oct(mode)}). Run: chmod 700 '{data}'"
        )


def check(
    env: dict[str, str] | None = None,
    *,
    require_exists: bool = True,
    probe: SystemProbe | None = None,
    repo: Path | None = None,
) -> DataDir:
    """Run every guard and return the verified data directory, or raise StorageGuardError."""
    env = dict(os.environ) if env is None else env
    probe = probe or SystemProbe()
    repo = repo if repo is not None else repo_root()

    raw = env.get(ENV_VAR, "").strip()
    if not raw:
        raise StorageGuardError(
            f"{ENV_VAR} is not set. Point it at your data folder on the encrypted external "
            "drive, e.g. export CAREERHUNTER_DATA_DIR=/Volumes/X10-Pro/CareerHunterData"
        )
    given = Path(raw).expanduser()
    if not given.is_absolute():
        raise StorageGuardError(f"{ENV_VAR} must be an absolute path, got {raw!r}.")
    data = given.resolve()

    _check_repo_separation(data, probe, repo.resolve() if repo else None)
    mount = _check_volume(data, probe)

    if require_exists:
        if not data.is_dir():
            raise StorageGuardError(
                f"Data directory {data} does not exist yet. Run: careerhunter init"
            )
        _check_permissions(data)
    return DataDir(root=data, mount_point=mount)


def create(datadir: DataDir) -> list[Path]:
    """Create the data directory and its subfolders. Returns what was newly created.

    The data directory itself is made without `parents=True`: if its parent is missing
    (for example the drive is not mounted) this fails instead of quietly building the
    path on the boot disk.
    """
    created: list[Path] = []
    if not datadir.root.exists():
        try:
            datadir.root.mkdir(mode=0o700)
        except FileNotFoundError as exc:
            raise StorageGuardError(
                f"Parent folder {datadir.root.parent} does not exist; refusing to create it."
            ) from exc
        created.append(datadir.root)
    os.chmod(datadir.root, 0o700)
    for name in SUBDIRS:
        sub = datadir.root / name
        if not sub.exists():
            sub.mkdir(mode=0o700)
            created.append(sub)
    return created
