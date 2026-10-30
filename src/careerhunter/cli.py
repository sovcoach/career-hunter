"""Command-line entry point: `careerhunter <command>`.

Every command runs the storage guard first. If the guard fails, the command does not
run and the exit code is 2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import shutil
import sqlite3
import sys
from collections.abc import Callable, Sequence

from careerhunter import __version__, db, secrets_store, storage_guard
from careerhunter.config import ConfigError, load_config, load_exclusions
from careerhunter.export import export, plural
from careerhunter.platform_checks import SystemProbe
from careerhunter.seed import SeedError, import_seed
from careerhunter.storage_guard import DataDir, StorageGuardError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GUARD = 2

TEMPLATES = (
    ("config.example.yaml", "config.yaml"),
    ("exclusions.example.yaml", "exclusions.yaml"),
    ("seed.example.yaml", "seed.yaml"),
)

FILEVAULT_WARNING = """
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!  WARNING: FileVault is {state} on this Mac's boot disk.
!!  The Keychain (which holds career-hunter's secrets) and this code live
!!  there. Turn it on: System Settings > Privacy & Security > FileVault.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _warn_filevault(probe: SystemProbe) -> None:
    state = probe.filevault_on()
    if state is not True:
        _err(FILEVAULT_WARNING.format(state="OFF" if state is False else "UNKNOWN"))


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def _open_db(datadir: DataDir) -> sqlite3.Connection:
    conn = db.connect(datadir.db_path)
    db.migrate(conn)
    return conn


def cmd_init(datadir: DataDir, args: argparse.Namespace) -> int:
    for path in storage_guard.create(datadir):
        print(f"created  {path}")
    repo = storage_guard.repo_root()
    for template, target in TEMPLATES:
        dest = datadir.root / target
        if dest.exists():
            print(f"kept     {dest} (already exists)")
            continue
        src = repo / template if repo else None
        if src is None or not src.is_file():
            _err(f"template {template} not found; create {dest} by hand")
            continue
        shutil.copyfile(src, dest)
        dest.chmod(0o600)
        print(f"created  {dest} (from {template}; edit it before seeding)")
    conn = _open_db(datadir)
    print(f"database {datadir.db_path} at schema version {db.schema_version(conn)}")
    conn.close()
    return EXIT_OK


def cmd_migrate(datadir: DataDir, args: argparse.Namespace) -> int:
    conn = db.connect(datadir.db_path)
    applied = db.migrate(conn)
    print(f"applied {applied or 'nothing'}; schema version {db.schema_version(conn)}")
    conn.close()
    return EXIT_OK


def cmd_seed(datadir: DataDir, args: argparse.Namespace) -> int:
    conn = _open_db(datadir)
    try:
        result = import_seed(conn, datadir.seed_path)
    finally:
        conn.close()
    for table in ("companies", "postings", "applications", "status_history", "activities"):
        new, old = result.inserted.get(table, 0), result.existing.get(table, 0)
        print(f"{table:15} {new:4} inserted  {old:4} already present")
    return EXIT_OK


def cmd_export(datadir: DataDir, args: argparse.Namespace) -> int:
    conn = _open_db(datadir)
    try:
        result = export(conn, datadir.exports, args.date_from, args.date_to)
    finally:
        conn.close()
    print(
        f"{plural(result.application_count, 'application', 'applications')}, "
        f"{plural(result.activity_count, 'activity', 'activities')}"
    )
    for path in (result.pdf, result.applications_csv, result.activities_csv):
        print(f"wrote {path}")
    return EXIT_OK


def cmd_secret(datadir: DataDir, args: argparse.Namespace) -> int:
    if args.action == "verify":
        print(f"{args.name}: {secrets_store.describe_secret(args.name)}")
        return EXIT_OK
    first = getpass.getpass(f"Paste {args.name} (input hidden): ")
    second = getpass.getpass("Paste it again to confirm: ")
    if first.strip() != second.strip():
        _err("The two entries did not match. Nothing was stored.")
        return EXIT_ERROR
    secrets_store.set_secret(args.name, first)
    print(f"{args.name}: stored in Keychain, {secrets_store.describe_secret(args.name)}")
    return EXIT_OK


def cmd_doctor(datadir: DataDir, args: argparse.Namespace) -> int:
    ok = True
    print(f"data dir        OK  {datadir.root} (volume {datadir.mount_point}, encrypted)")
    if datadir.db_path.exists():
        loose = db.loose_permissions(datadir.db_path)
        conn = _open_db(datadir)  # connect() tightens the database files to 0600
        print(f"database        OK  schema version {db.schema_version(conn)}")
        conn.close()
        if loose:
            print(f"db permissions  FIXED {', '.join(loose)} -> 0600")
        else:
            print("db permissions  OK  0600")
    else:
        print("database        --  not created yet (run: careerhunter init)")
    for path, loader in (
        (datadir.config_path, load_config),
        (datadir.exclusions_path, load_exclusions),
    ):
        if not path.exists():
            print(f"{path.name:15} --  missing")
            continue
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            ok = False
            print(f"{path.name:15} BAD readable by others ({oct(mode)}); run: chmod 600 '{path}'")
            continue
        try:
            loader(path)
            print(f"{path.name:15} OK")
        except ConfigError as exc:
            ok = False
            print(f"{path.name:15} BAD {exc}")
    for name in sorted(secrets_store.ALLOWED_NAMES):
        print(f"secret          {name}: {secrets_store.describe_secret(name)}")
    return EXIT_OK if ok else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="careerhunter",
        description="Self-hosted job-search tracker and EI job-search record.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data directory, templates and database")
    sub.add_parser("migrate", help="apply pending database migrations")
    sub.add_parser("seed", help="import seed.yaml from the data directory (safe to re-run)")
    p = sub.add_parser("export", help="write the EI record (CSV + PDF) for a date range")
    p.add_argument("--from", dest="date_from", type=_date, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", type=_date, required=True, metavar="YYYY-MM-DD")
    p = sub.add_parser("secret", help="store or check a secret in the macOS Keychain")
    p.add_argument("action", choices=("set", "verify"))
    p.add_argument("name", choices=sorted(secrets_store.ALLOWED_NAMES))
    sub.add_parser("doctor", help="check the setup and report what is missing")
    return parser


COMMANDS: dict[str, Callable[[DataDir, argparse.Namespace], int]] = {
    "init": cmd_init,
    "migrate": cmd_migrate,
    "seed": cmd_seed,
    "export": cmd_export,
    "secret": cmd_secret,
    "doctor": cmd_doctor,
}


def main(argv: Sequence[str] | None = None, probe: SystemProbe | None = None) -> int:
    args = build_parser().parse_args(argv)
    probe = probe or SystemProbe()
    try:
        datadir = storage_guard.check(require_exists=args.command != "init", probe=probe)
    except StorageGuardError as exc:
        _err(f"REFUSING TO START: {exc}")
        return EXIT_GUARD
    _warn_filevault(probe)
    try:
        return COMMANDS[args.command](datadir, args)
    except (
        SeedError,
        ConfigError,
        secrets_store.SecretError,
        db.MigrationError,
        ValueError,
    ) as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    except FileExistsError as exc:
        _err(f"error: refusing to overwrite {exc.filename}")
        return EXIT_ERROR
