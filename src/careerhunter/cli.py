"""Command-line entry point: `careerhunter <command>`.

Every command runs the storage guard first. If the guard fails, the command does not
run and the exit code is 2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from careerhunter import __version__, claims, db, secrets_store, storage_guard
from careerhunter.claims import ActiveClaim, ClaimError, ClaimSettings, settings_from_config
from careerhunter.config import ConfigError, load_config, load_exclusions
from careerhunter.export import export, plural
from careerhunter.ledger import PartialWriteError, reconcile
from careerhunter.platform_checks import SystemProbe
from careerhunter.seed import SeedError, Tally, import_seed
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


def _claim_settings(datadir: DataDir) -> ClaimSettings:
    """The EI claim settings from config.yaml. No config file means no active claim."""
    if not datadir.config_path.exists():
        return ClaimSettings()
    return settings_from_config(load_config(datadir.config_path))


@dataclass
class Writable:
    search: sqlite3.Connection
    search_applied: list[int]
    active: ActiveClaim | None


@contextmanager
def _writable(datadir: DataDir) -> Iterator[Writable]:
    """Open search.db and the active claim's record for writing, after every check.

    Refuses while any earlier claim's record is unsealed, or a claim file is unknown.
    """
    settings = _claim_settings(datadir)
    claims.require_write_ready(datadir, settings)
    for warning in claims.warnings(settings, dt.date.today()):
        _err(f"warning: {warning}")
    search = db.connect(datadir.search_db_path)
    try:
        applied = db.migrate(search, "search")
        with claims.active_claim(datadir, settings) as active:
            for message in active.messages if active else []:
                print(message)
            yield Writable(search, applied, active)
    finally:
        search.close()


def _describe_active(active: ActiveClaim | None) -> str:
    if active is None:
        return "no active EI claim (set ei.active_claim in config.yaml; see docs/SETUP.md)"
    return f"EI claim {active.claim.label} ({active.claim.period})"


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
    with _writable(datadir) as w:
        print(f"database {datadir.search_db_path} at schema version {db.schema_version(w.search)}")
        if w.active:
            verb = "created " if w.active.created else "database"
            print(f"{verb} {w.active.path} at schema version {db.schema_version(w.active.conn)}")
        print(_describe_active(w.active))
    return EXIT_OK


def cmd_migrate(datadir: DataDir, args: argparse.Namespace) -> int:
    with _writable(datadir) as w:
        print(
            f"{datadir.search_db_path.name}: applied {w.search_applied or 'nothing'}; "
            f"schema version {db.schema_version(w.search)}"
        )
        if w.active:
            done = "created" if w.active.created else f"applied {w.active.applied or 'nothing'}"
            print(
                f"{w.active.path.name}: {done}; schema version {db.schema_version(w.active.conn)}"
            )
        print(_describe_active(w.active))
    return EXIT_OK


def _print_tally(title: str, tally: Tally, tables: tuple[str, ...]) -> None:
    print(title)
    for table in tables:
        new, old = tally.inserted.get(table, 0), tally.existing.get(table, 0)
        print(f"  {table:17} {new:4} inserted  {old:4} already present")


def cmd_seed(datadir: DataDir, args: argparse.Namespace) -> int:
    with _writable(datadir) as w:
        result = import_seed(
            w.search, w.active, datadir.seed_path, route_out_of_claim=args.route_out_of_claim
        )
        print(_describe_active(w.active))
        _print_tally(
            datadir.search_db_path.name,
            result.search,
            ("companies", "postings", "applications", "decision_history"),
        )
        if w.active:
            _print_tally(
                w.active.path.name, result.ei, ("applications", "status_history", "activities")
            )
    for warning in result.warnings:
        print(f"warning: {warning}")
    for entry in result.search_only:
        print(f"search.db only: {entry}")
    return EXIT_OK


def cmd_export(datadir: DataDir, args: argparse.Namespace) -> int:
    bad = claims.label_problem(args.claim)
    if bad:
        raise ValueError(bad)
    path = datadir.ei_record_path(args.claim)
    if not path.is_file():
        raise ValueError(f"there is no record for claim {args.claim} ({path.name})")
    conn = db.connect_read_only(path)
    try:
        result = export(conn, datadir.exports, args.date_from, args.date_to)
    finally:
        conn.close()
    print(
        f"EI claim {result.claim}: "
        f"{plural(result.application_count, 'application', 'applications')}, "
        f"{plural(result.activity_count, 'activity', 'activities')}"
    )
    for path in (result.pdf, result.applications_csv, result.activities_csv):
        print(f"wrote {path}")
    return EXIT_OK


def cmd_claim(datadir: DataDir, args: argparse.Namespace) -> int:
    settings = _claim_settings(datadir)
    plan = claims.plan_seal(datadir, settings, args.label, dt.date.today())
    print(plan.describe())
    try:
        typed = input("Type the claim label again to confirm: ").strip()
    except EOFError:
        typed = ""
    if typed != args.label:
        _err("The label did not match. Nothing was sealed.")
        return EXIT_ERROR
    claims.seal(plan)
    print(f"sealed   {plan.path} (read-only; keep until {claims.keep_until(plan.end)})")
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


class _Report:
    """doctor's output: one line per check, and whether anything failed."""

    def __init__(self) -> None:
        self.ok = True

    def line(self, name: str, status: str, detail: str = "") -> None:
        if status == "BAD":
            self.ok = False
        print(f"{name:15} {status:<3} {detail}".rstrip())


def _doctor_schema(
    report: _Report, name: str, conn: sqlite3.Connection, track: str, *, sealed: bool = False
) -> bool:
    """Report the file's identity and schema version. True if it is fit to read."""
    found = db.file_track(conn)
    if found != track:
        report.line(name, "BAD", f"not a {track} database (identity: {found or 'none'})")
        return False
    version, latest = db.schema_version(conn), db.latest_version(track)
    if version > latest:
        report.line(name, "BAD", f"schema version {version} is newer than this code ({latest})")
        return False
    if version < latest and not sealed:
        report.line(
            name, "BAD", f"schema version {version}, expected {latest}; run: careerhunter migrate"
        )
        return False
    report.line(name, "OK", f"schema version {version}")
    return True


def _tighten(path: Path) -> list[str]:
    """Fix loose database file modes before anything opens the file, and say which."""
    loose = db.loose_permissions(path)
    if loose:
        db.tighten_permissions(path)
    return loose


def _doctor_permissions(report: _Report, name: str, loose: list[str]) -> None:
    if loose:
        report.line(name, "FIXED", f"{', '.join(loose)} -> 0600")
    else:
        report.line(name, "OK", "0600")


def repo_push_checks(repo: Path) -> list[tuple[str, str]]:
    """(status, detail) for the checkout's push safety settings (ADR-011): `push.default`
    and the pre-push guard. Problems are warnings: they concern the code, not the data."""

    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        return result.stdout.strip()

    found = []
    push_default = git("config", "--get", "push.default")
    if push_default == "current":
        found.append(("OK", "push.default is current"))
    else:
        shown = push_default or "unset"
        found.append(("WARN", f"push.default is {shown}, not current; run: make hooks"))
    hook = (repo / git("rev-parse", "--git-path", "hooks") / "pre-push").resolve()
    guard = (repo / "scripts" / "pre_push_guard.py").resolve()
    if hook == guard:
        found.append(("OK", "pre-push guard installed"))
    else:
        found.append(("WARN", "the pre-push guard isn't installed; run: make hooks"))
    return found


def _doctor_private_folder(report: _Report, name: str, folder: Path) -> None:
    """Tighten anything in `folder` that others could read: files to 0600, folders to 0700.

    Symlinks are left alone and not followed, so a link can't point the fix elsewhere.
    """
    if not folder.is_dir():
        report.line(name, "--", "missing (run: careerhunter init)")
        return
    files, loose = 0, []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            continue
        wanted = 0o700 if path.is_dir() else 0o600
        files += not path.is_dir()
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(path, wanted)
            loose.append(f"{path.relative_to(folder)} ({oct(mode)})")
    if loose:
        report.line(name, "FIXED", f"{', '.join(loose)} -> owner-only")
    else:
        report.line(name, "OK", f"{plural(files, 'file', 'files')}, all owner-only")


def _doctor_legacy(report: _Report, path: Path) -> None:
    try:
        conn = db.connect_read_only(path)
        try:
            tables = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'"
                )
            ]
            rows = sum(
                conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]  # noqa: S608
                for t in tables
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        report.line(path.name, "BAD", f"v0.1.0 database that can't be read ({exc})")
        return
    if rows:
        report.line(
            path.name,
            "BAD",
            f"v0.1.0 database holding {rows} rows, which this version doesn't read. Keep it "
            "safe and ask before removing it",
        )
    else:
        report.line(path.name, "WARN", "empty v0.1.0 database, no longer used; you can delete it")


def _doctor_claim_file(
    report: _Report, datadir: DataDir, settings: ClaimSettings, path: Path
) -> sqlite3.Connection | None:
    """Check one ei_record file. Returns an open read-only connection for reconciling."""
    label = claims.label_of(path)
    name = f"claim {label}"
    if claims.label_problem(label):
        report.line(path.name, "BAD", "not a valid claim file name")
        return None
    claim = settings.get(label)
    if claim is None:
        report.line(name, "BAD", f"{path.name} is not listed under ei.claims in config.yaml")
        return None
    is_active = settings.active is not None and settings.active.label == label
    loose = _tighten(path) if is_active else []
    conn = db.connect_read_only(path)
    try:
        file_claim = claims.read_file_claim(conn)
        sealed = file_claim is not None and file_claim.sealed_at is not None
        if not _doctor_schema(report, name, conn, "ei_record", sealed=sealed):
            conn.close()
            return None
        problem = claims.mismatch(file_claim, claim, path)
        if problem:
            report.line(name, "BAD", problem)
            conn.close()
            return None
        assert file_claim is not None  # noqa: S101 - mismatch() covers None
        if is_active:
            if sealed:
                report.line(name, "BAD", "the active claim's record is sealed")
            else:
                report.line(name, "OK", f"active, {claim.period}")
            _doctor_permissions(report, name, loose)
        else:
            _doctor_earlier_claim(report, name, path, file_claim)
    except BaseException:
        conn.close()
        raise
    return conn


def _doctor_earlier_claim(
    report: _Report, name: str, path: Path, file_claim: claims.FileClaim
) -> None:
    if file_claim.sealed_at is None:
        report.line(
            name,
            "BAD",
            "not the active claim, but not sealed. "
            f"Run: careerhunter claim seal {file_claim.label}",
        )
        return
    mode = path.stat().st_mode & 0o777
    leftovers = [f.name for f in db.db_files(path)[1:] if f.exists()]
    if mode & 0o277:
        report.line(name, "BAD", f"sealed, but {path.name} is not read-only ({oct(mode)})")
    elif leftovers:
        report.line(name, "BAD", f"sealed, but {', '.join(leftovers)} still exist")
    else:
        end = dt.date.fromisoformat(str(file_claim.end))
        report.line(
            name,
            "OK",
            f"sealed {file_claim.sealed_at}, read-only; keep until {claims.keep_until(end)}",
        )


def cmd_doctor(datadir: DataDir, args: argparse.Namespace) -> int:
    """Check everything and report. Reads only, apart from tightening file modes."""
    report = _Report()
    report.line("data dir", "OK", f"{datadir.root} (volume {datadir.mount_point}, encrypted)")

    settings: ClaimSettings | None = None
    for path, loader in (
        (datadir.config_path, load_config),
        (datadir.exclusions_path, load_exclusions),
    ):
        if not path.exists():
            report.line(path.name, "--", "missing")
            continue
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            report.line(
                path.name, "BAD", f"readable by others ({oct(mode)}); run: chmod 600 '{path}'"
            )
            continue
        try:
            doc = loader(path)
        except ConfigError as exc:
            report.line(path.name, "BAD", str(exc))
            continue
        report.line(path.name, "OK")
        if path == datadir.config_path:
            settings = settings_from_config(doc)

    if settings is None:
        report.line("ei claim", "--", "unknown until config.yaml is valid")
        settings = ClaimSettings()
    elif settings.active:
        report.line("ei claim", "OK", f"{settings.active.label} active ({settings.active.period})")
    else:
        report.line("ei claim", "--", "none active (set ei.active_claim in config.yaml)")
    for warning in claims.warnings(settings, dt.date.today()):
        report.line("ei claim", "WARN", warning)

    if datadir.legacy_db_path.exists():
        _doctor_legacy(report, datadir.legacy_db_path)

    search: sqlite3.Connection | None = None
    if datadir.search_db_path.exists():
        loose = _tighten(datadir.search_db_path)
        search = db.connect_read_only(datadir.search_db_path)
        if _doctor_schema(report, "search.db", search, "search"):
            _doctor_permissions(report, "search.db", loose)
        else:
            search.close()
            search = None
    else:
        report.line("search.db", "--", "not created yet (run: careerhunter init)")

    records: dict[str, sqlite3.Connection] = {}
    unreadable = search is None and datadir.search_db_path.exists()
    try:
        for path in datadir.ei_record_files():
            conn = _doctor_claim_file(report, datadir, settings, path)
            if conn is None:
                unreadable = True
            else:
                records[claims.label_of(path)] = conn
        if settings.active and not datadir.ei_record_path(settings.active.label).exists():
            name = f"claim {settings.active.label}"
            report.line(
                name, "BAD", "the active claim has no record file; run: careerhunter migrate"
            )
        if unreadable:
            report.line("reconcile", "--", "skipped until the problems above are fixed")
        elif search is not None:
            gaps = reconcile(search, records)
            for gap in gaps:
                report.line("reconcile", "BAD", f"{gap.correlation_id}: {gap.problem}")
            if not gaps:
                report.line(
                    "reconcile",
                    "OK",
                    f"search.db and {plural(len(records), 'claim record', 'claim records')} agree",
                )
    finally:
        for conn in records.values():
            conn.close()
        if search is not None:
            search.close()

    _doctor_private_folder(report, "exports/", datadir.exports)
    _doctor_private_folder(report, "snapshots/", datadir.snapshots)

    repo = storage_guard.repo_root()
    if repo is not None and (repo / ".git").exists():
        for status, detail in repo_push_checks(repo):
            report.line("git push", status, detail)

    for name in sorted(secrets_store.ALLOWED_NAMES):
        print(f"secret          {name}: {secrets_store.describe_secret(name)}")
    return EXIT_OK if report.ok else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="careerhunter",
        description="Self-hosted job-search tracker and EI job-search record.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data directory, templates and database")
    sub.add_parser("migrate", help="apply pending database migrations")
    p = sub.add_parser("seed", help="import seed.yaml from the data directory (safe to re-run)")
    p.add_argument(
        "--route-out-of-claim-to-search",
        dest="route_out_of_claim",
        action="store_true",
        help="record applications dated outside the active claim in search.db only, "
        "instead of refusing the import",
    )
    p = sub.add_parser("export", help="write one claim's EI record (CSV + PDF) for a date range")
    p.add_argument("--claim", required=True, metavar="LABEL", help="the claim, e.g. 2026-27")
    p.add_argument("--from", dest="date_from", type=_date, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", type=_date, required=True, metavar="YYYY-MM-DD")
    p = sub.add_parser("secret", help="store or check a secret in the macOS Keychain")
    p.add_argument("action", choices=("set", "verify"))
    p.add_argument("name", choices=sorted(secrets_store.ALLOWED_NAMES))
    p = sub.add_parser("claim", help="manage EI claim records")
    claim_sub = p.add_subparsers(dest="claim_action", required=True)
    p = claim_sub.add_parser("seal", help="permanently close an earlier claim's record")
    p.add_argument("label", help="the claim to seal, e.g. 2025-26")
    sub.add_parser("doctor", help="check the setup and report what is missing")
    return parser


COMMANDS: dict[str, Callable[[DataDir, argparse.Namespace], int]] = {
    "init": cmd_init,
    "migrate": cmd_migrate,
    "seed": cmd_seed,
    "export": cmd_export,
    "claim": cmd_claim,
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
        ClaimError,
        PartialWriteError,
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
