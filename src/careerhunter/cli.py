"""Command-line entry point: `careerhunter <command>`.

Every command runs the storage guard first. If the guard fails, the command does not
run and the exit code is 2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from careerhunter import (
    __version__,
    ats,
    bot,
    claims,
    db,
    demo,
    discover,
    embed,
    embed_eval,
    lanes,
    resume,
    search_store,
    secrets_store,
    selfsourced,
    service,
    storage_guard,
    watch,
)
from careerhunter import report as claim_report
from careerhunter.bot import BotStartupError
from careerhunter.claims import ActiveClaim, ClaimError, ClaimSettings, settings_from_config
from careerhunter.config import (
    ConfigError,
    discovery_enabled,
    discovery_interval_minutes,
    health_failure_runs,
    health_unproductive_runs,
    learning_labelled_examples,
    load_config,
    load_exclusions,
    off_lane_min_fit,
    telegram_chat_id,
    timezone_name,
    watch_interval_minutes,
)
from careerhunter.config import today as config_today
from careerhunter.deadline import Deadline
from careerhunter.export import export, plural
from careerhunter.fetch import JSON, Fetched, post_json
from careerhunter.fetch import fetch as fetch_url
from careerhunter.filters import PostingText
from careerhunter.ledger import PartialWriteError, reconcile
from careerhunter.platform_checks import SystemProbe
from careerhunter.runlog import RunLog, RunLogError
from careerhunter.score import Score, score_posting
from careerhunter.score import default_transport as score_transport
from careerhunter.scout_prompt import render
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


def _config_doc(datadir: DataDir) -> dict[str, Any] | None:
    """The validated config.yaml, or None if there isn't one yet."""
    if not datadir.config_path.exists():
        return None
    return load_config(datadir.config_path)


def _claim_settings(datadir: DataDir) -> ClaimSettings:
    """The EI claim settings from config.yaml. No config file means no active claim."""
    doc = _config_doc(datadir)
    return settings_from_config(doc) if doc else ClaimSettings()


@dataclass
class Writable:
    search: sqlite3.Connection
    search_applied: list[int]
    active: ActiveClaim | None
    today: dt.date


@contextmanager
def _writable(datadir: DataDir) -> Iterator[Writable]:
    """Open search.db and the active claim's record for writing, after every check.

    Refuses while any earlier claim's record is unsealed, or a claim file is unknown.
    """
    doc = _config_doc(datadir)
    settings = settings_from_config(doc) if doc else ClaimSettings()
    today = config_today(doc)
    claims.require_write_ready(datadir, settings)
    for warning in claims.warnings(settings, today):
        _err(f"warning: {warning}")
    search = db.connect(datadir.search_db_path)
    try:
        applied = db.migrate(search, "search")
        with claims.active_claim(datadir, settings) as active:
            for message in active.messages if active else []:
                print(message)
            yield Writable(search, applied, active, today)
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
            w.search,
            w.active,
            datadir.seed_path,
            w.today,
            route_out_of_claim=args.route_out_of_claim,
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


def cmd_log(datadir: DataDir, args: argparse.Namespace) -> int:
    """A self-sourced application in one step (#34): the posting and the application go
    to both layers under one correlation id, in one ledger call.

    Fails closed like the bot's `/log`: without a readable config.yaml there is no date
    and no claim check, so nothing is fetched or written.
    """
    if not datadir.config_path.is_file():
        _err("error: config.yaml is missing, so nothing was recorded")
        return EXIT_ERROR
    if (args.company is None) != (args.title is None):
        _err("error: give --company and --title together, or neither")
        return EXIT_ERROR
    doc = load_config(datadir.config_path)
    today = config_today(doc)
    try:
        method = selfsourced.parse_method(args.method)
        dating = selfsourced.parse_day(args.date, today)
        posting = selfsourced.describe(
            args.url, fetch_page, today, company=args.company, title=args.title
        )
        with _writable(datadir) as w:
            logged = selfsourced.record(
                datadir.root, w.search, w.active, posting, method, dating, today, fetch_page
            )
    except selfsourced.NamesMissing as exc:
        _err(f"error: {exc} Add --company and --title.")
        return EXIT_ERROR
    except selfsourced.LogError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    where = f"EI claim {logged.ei_claim}" if logged.ei_claim else "search.db only, no active claim"
    print(f"Recorded: {logged.company} — {logged.title} ({where}).")
    print("Job ad saved." if logged.snapshot_saved else "No copy of the job ad was saved.")
    if logged.earlier:
        print(logged.earlier)
    return EXIT_OK


def fetch_page(url: str) -> Fetched:
    """The safe fetch (ADR-016) for `careerhunter log`. Tests replace it."""
    return fetch_url(url)


def cmd_report(datadir: DataDir, args: argparse.Namespace) -> int:
    """Join one claim's EI record to search.db, both read-only, into a private report.

    The only command that attaches one database to another (ADR-014 as amended). It
    writes reports/report-<claim>-<utc>.txt (0600) and prints only that relative path.
    """
    bad = claims.label_problem(args.claim)
    if bad:
        raise ValueError(bad)
    if not datadir.search_db_path.is_file():
        _err("error: search.db is missing; run careerhunter init")
        return EXIT_ERROR
    try:
        conn = claim_report.connect(datadir.search_db_path, datadir.ei_record_path(args.claim))
    except claim_report.ReportError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    except sqlite3.DatabaseError:
        _err(f"error: the record for claim {args.claim} can't be read as a claim record")
        return EXIT_ERROR
    try:
        built = claim_report.build(conn, args.claim)
    finally:
        conn.close()
    now = _utc_now()
    path = claim_report.write(datadir.root, args.claim, claim_report.render(built, now), now)
    print(path.relative_to(datadir.root).as_posix())
    return EXIT_OK


def cmd_claim(datadir: DataDir, args: argparse.Namespace) -> int:
    doc = _config_doc(datadir)
    settings = settings_from_config(doc) if doc else ClaimSettings()
    plan = claims.plan_seal(datadir, settings, args.label, config_today(doc))
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


def cmd_bot(datadir: DataDir, args: argparse.Namespace) -> int:
    if args.bot_action == "discover":
        chats = bot.discover(datadir)
        if not chats:
            print("No messages waiting. Send the bot any message from your phone, then retry.")
        for chat_id, kind in chats:
            print(f"chat {chat_id} ({kind})")
        print("Put your private chat's id in config.yaml as telegram.chat_id.")
        return EXIT_OK
    return bot.run(datadir)


def _service_config(datadir: DataDir) -> dict[str, Any] | None:
    if not datadir.config_path.is_file():
        return None
    return load_config(datadir.config_path)


def _print_service(name: str, current: service.ServiceStatus, *, stay_running: bool) -> None:
    if not current.installed:
        print(f"{name}: not installed (run: careerhunter service install)")
    elif not current.loaded:
        print(f"{name}: installed but not loaded")
    elif current.running:
        print(f"{name}: running (pid {current.pid})")
    elif stay_running:
        print(f"{name}: installed but not running (last exit {current.last_exit or 'unknown'})")
    else:
        print(f"{name}: loaded")


def cmd_service(datadir: DataDir, args: argparse.Namespace) -> int:
    if args.service_action == "install":
        doc = _service_config(datadir)
        minutes = watch_interval_minutes(doc)
        retired = service.retire_discovery_agent()
        if retired is not None:
            print(f"removed {retired}; discovery now runs inside the watch agent")
        try:
            installed = service.install(datadir.root, watch_interval_minutes=minutes)
        except service.ServiceError:
            if retired is None:
                raise
            installed = []  # the bot and watch agents were already there: migrated
        for path in installed:
            if path.name == f"{service.WATCH_LABEL}.plist":
                print(f"installed {path}; careerhunter watch runs now and every {minutes} minutes")
            else:
                print(f"installed {path}; the bot starts now and at every login")
    elif args.service_action == "uninstall":
        removed = service.uninstall()
        print("uninstalled" if removed else "not installed")
    else:
        doc = _service_config(datadir)
        _print_service("bot", service.status(), stay_running=True)
        _print_service("watch", service.status(label=service.WATCH_LABEL), stay_running=False)
        if service.discover_plist_path().exists():
            print(
                "discovery: the old separate agent is installed (run: careerhunter service install)"
            )
        elif discovery_enabled(doc):
            print("discovery: runs inside the watch agent")
        else:
            print("discovery: off")
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


HEARTBEAT_STALE = dt.timedelta(minutes=5)

Clock = Callable[[], dt.datetime]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _doctor_bot(
    report: _Report, datadir: DataDir, config_doc: dict[str, Any] | None, now: dt.datetime
) -> None:
    """The Telegram chat setting, and whether the bot service is up and polling."""
    if config_doc is not None:
        if telegram_chat_id(config_doc) is None:
            report.line(
                "telegram", "--", "telegram.chat_id not set (see: careerhunter bot discover)"
            )
        else:
            report.line("telegram", "OK", "chat set")
    current = service.status()
    if not current.installed:
        report.line("bot service", "--", "not installed (run: careerhunter service install)")
        return
    if current.running:
        report.line("bot service", "OK", f"running (pid {current.pid})")
    else:
        detail = f"installed but not running (last exit {current.last_exit or 'unknown'})"
        report.line("bot service", "BAD", f"{detail}; see logs/bot.log")
    heartbeat = datadir.logs / "bot.heartbeat"
    try:
        beat = dt.datetime.fromisoformat(heartbeat.read_text().strip())
    except (OSError, ValueError):
        report.line("bot heartbeat", "BAD", "none yet; the bot hasn't completed a poll")
        return
    age = now - beat
    if age > HEARTBEAT_STALE:
        minutes = int(age.total_seconds() // 60)
        report.line("bot heartbeat", "BAD", f"last poll {minutes} min ago; see logs/bot.log")
    else:
        report.line("bot heartbeat", "OK", "polled within the last 5 minutes")


def _doctor_watch(
    report: _Report, datadir: DataDir, config_doc: dict[str, Any] | None, now: dt.datetime
) -> None:
    """Whether the watch agent is loaded, and how old the last finished run is."""
    current = service.status(label=service.WATCH_LABEL)
    if not current.installed:
        report.line("watch service", "--", "not installed (run: careerhunter service install)")
    elif not current.loaded:
        report.line("watch service", "BAD", "installed but not loaded")
    elif current.running and current.pid is not None:
        report.line("watch service", "OK", f"running (pid {current.pid})")
    else:
        report.line("watch service", "OK", "loaded")
    if current.installed:
        _doctor_watch_interval(report, config_doc)
    _doctor_watch_success(report, datadir, config_doc, now)


def _doctor_interval(
    report: _Report,
    config_doc: dict[str, Any] | None,
    *,
    name: str,
    path: Path,
    expected: int,
    noun: str,
) -> None:
    if config_doc is None:
        return
    try:
        installed = plistlib.loads(path.read_bytes()).get("StartInterval")
    except (OSError, plistlib.InvalidFileException):
        report.line(name, "WARN", f"could not read the {noun} plist; reinstall it")
        return
    if installed != expected:
        report.line(
            name,
            "WARN",
            f"plist has {installed}s, config asks for {expected}s; reinstall the {noun} service",
        )


def _doctor_watch_interval(report: _Report, config_doc: dict[str, Any] | None) -> None:
    _doctor_interval(
        report,
        config_doc,
        name="watch interval",
        path=service.watch_plist_path(),
        expected=watch_interval_minutes(config_doc) * 60,
        noun="watch",
    )


def _doctor_stamp(
    report: _Report,
    datadir: DataDir,
    *,
    name: str,
    filename: str,
    limit: dt.timedelta,
    now: dt.datetime,
) -> None:
    path = datadir.logs / filename
    try:
        stamp = dt.datetime.fromisoformat(path.read_text().strip())
    except FileNotFoundError:
        report.line(name, "--", "no successful run yet")
        return
    except (OSError, ValueError):
        report.line(name, "BAD", "last success stamp is unreadable")
        return
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    age = now - stamp.astimezone(dt.UTC)
    minutes = max(0, int(age.total_seconds() // 60))
    detail = f"last success {minutes} min ago"
    report.line(name, "BAD" if age > limit else "OK", detail)


def _doctor_watch_success(
    report: _Report, datadir: DataDir, config_doc: dict[str, Any] | None, now: dt.datetime
) -> None:
    _doctor_stamp(
        report,
        datadir,
        name="watch",
        filename="watch.success",
        limit=dt.timedelta(minutes=2 * watch_interval_minutes(config_doc)),
        now=now,
    )


def _doctor_discover(
    report: _Report, datadir: DataDir, config_doc: dict[str, Any] | None, now: dt.datetime
) -> None:
    """Discovery runs inside the watch agent (#40). A null URL is off.

    The separate agent of earlier versions left installed is a warning, which does not
    fail doctor. The success stamp is checked against two discovery intervals, or two
    watch intervals when those are longer, since a pass can only start in a watch run.
    """
    if service.discover_plist_path().exists():
        report.line(
            "discovery agent",
            "WARN",
            "the old separate agent is installed (run: careerhunter service install)",
        )
    if not discovery_enabled(config_doc):
        report.line("discovery", "--", "off")
        return
    every = max(discovery_interval_minutes(config_doc), watch_interval_minutes(config_doc))
    _doctor_stamp(
        report,
        datadir,
        name="discovery",
        filename="discover.success",
        limit=dt.timedelta(minutes=2 * every),
        now=now,
    )


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


def cmd_watch(datadir: DataDir, args: argparse.Namespace) -> int:
    """Fetch Greenhouse, Lever and Ashby boards once, then exit.

    Every line goes to logs/watch.log as it happens, then to stderr. Only the run log,
    config.yaml, or search.db failing stops the run with exit 1.
    """
    try:
        log = RunLog(datadir.logs / "watch.log")
    except RunLogError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    with log:
        try:
            return _watch(datadir, log)
        except RunLogError as exc:
            _err(f"error: {exc}")
            return EXIT_ERROR
        except ConfigError as exc:
            _note(log, f"watch: aborted: ConfigError: {exc}")
            raise
        except Exception as exc:
            _note(log, _with_traceback("watch: aborted", exc))
            raise


def _watch(datadir: DataDir, log: RunLog) -> int:
    if not datadir.config_path.is_file():
        return _abort(log, "watch", "config.yaml is missing")
    if not datadir.search_db_path.is_file():
        return _abort(log, "watch", "search.db is missing; run careerhunter init")
    doc = load_config(datadir.config_path)
    entries = doc.get("watchlist", [])
    if not isinstance(entries, list):
        raise ConfigError(datadir.config_path, ["watchlist must be a list"])
    exclusions: dict[str, Any] = {}
    if datadir.exclusions_path.is_file():
        exclusions = load_exclusions(datadir.exclusions_path)

    # One budget for the whole run, shared by every client the run calls.
    budget = Deadline(watch.RUN_BUDGET)

    def on_crash(who: str, exc: Exception) -> None:
        log.note(_with_traceback(f"watch: {who}", exc))

    try:
        conn = db.connect(datadir.search_db_path)
    except sqlite3.Error as exc:
        return _db_abort(log, "watch", exc)
    try:
        db.migrate(conn, "search")
        # The resume is loaded once and given to both the scorer and the run, so the
        # hash the run stores is the one the prompt was built with.
        loaded: resume.Resume | None = None
        problem: str | None = None
        try:
            loaded = resume.load(datadir.root, conn, doc)
        except resume.ResumeError as exc:
            problem = str(exc)

        # The operator's labelled postings, read once per run (#43). They reach the
        # local model's request only.
        examples = search_store.labelled_examples(conn, learning_labelled_examples(doc))

        def scorer(posting: PostingText) -> Score | None:
            return score_posting(
                posting,
                doc,
                transport=partial(score_transport, budget=budget),
                resume=loaded,
                examples=examples,
            )

        watch.run(
            conn,
            entries,
            today=config_today(doc).isoformat(),
            fetcher=partial(fetch_url, budget=budget, max_bytes=watch.BOARD_MAX_BYTES, accept=JSON),
            poster=partial(post_json, budget=budget, max_bytes=watch.BOARD_MAX_BYTES),
            config=doc,
            exclusions=exclusions,
            scorer=scorer,
            data_dir=datadir.root,
            embed_transport=partial(embed.default_transport, budget=budget),
            on_crash=on_crash,
            emit=log.append,
            budget=budget,
            resume=loaded,
            resume_problem=problem,
            discovery=partial(_discovery_pass, datadir, doc, exclusions, budget),
        )
    except (sqlite3.Error, db.MigrationError) as exc:
        _rollback(conn)
        return _db_abort(log, "watch", exc)
    finally:
        _rollback(conn)
        conn.close()
    _record_watch_success(datadir)
    return EXIT_OK


BUSY = "discover: another pass started less than 150 seconds ago; skipped"


def _confirmer(share: Deadline) -> discover.Confirm:
    """One board-API request per discovered board, under the pass's share of the budget."""

    def confirm(board: ats.Board) -> ats.Confirmed:
        client = ats.Client(
            partial(fetch_url, budget=share, max_bytes=ats.BOARD_MAX_BYTES, accept=JSON),
            poster=partial(post_json, budget=share, max_bytes=ats.BOARD_MAX_BYTES),
        )
        return ats.confirm_board(board, client)

    return confirm


def _discovery_pass(
    datadir: DataDir,
    doc: dict[str, Any],
    exclusions: dict[str, Any],
    budget: Deadline,
    conn: sqlite3.Connection,
) -> discover.Pass:
    """One discovery pass inside the watch run, when it is on and due (#40).

    Due means no pass was claimed in the last `discovery.interval_minutes`; the claim is
    atomic (`discover.claim_pass`). The pass gets at most `discover.PASS_SECONDS` of the
    run budget, and writes `logs/discover.success` once it has run, a miss included.
    """
    if not discovery_enabled(doc):
        return discover.Pass([])
    gap = dt.timedelta(minutes=discovery_interval_minutes(doc))
    if not discover.claim_pass(conn, discover._utc_now(), gap):
        return discover.Pass([])
    share = Deadline(discover.PASS_SECONDS).sooner(budget)
    outcome = discover.run(
        conn,
        doc,
        today=config_today(doc).isoformat(),
        transport=partial(discover.default_transport, budget=share),
        confirm=_confirmer(share),
        exclusions=exclusions,
    )
    lines = [outcome.notice] if outcome.notice else []
    lines += outcome.notes
    lines.append(f"discover: {len(outcome.rows)} queued, {len(outcome.postings)} postings found")
    _record_discover_success(datadir)
    return discover.Pass(lines, outcome.postings)


def cmd_discover(datadir: DataDir, args: argparse.Namespace) -> int:
    """Queue companies, or approve or reject one pending row. Writes search.db only.

    A search run writes its lines to logs/discover.log as well; list, approve and
    reject write no log file.
    """
    action = args.discover_action
    candidate_id = args.candidate_id
    if action in ("approve", "reject"):
        if not isinstance(candidate_id, int):
            _err(f"error: discover {action} needs a candidate id")
            return EXIT_ERROR
    elif candidate_id is not None:
        _err(f"error: discover {action} takes no candidate id")
        return EXIT_ERROR
    if action != "run":
        return _discover_by_hand(datadir, action, candidate_id)
    try:
        log = RunLog(datadir.logs / "discover.log")
    except RunLogError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    with log:
        try:
            return _discover_run(datadir, log)
        except RunLogError as exc:
            _err(f"error: {exc}")
            return EXIT_ERROR
        except ConfigError as exc:
            _note(log, f"discover: aborted: ConfigError: {exc}")
            raise
        except Exception as exc:
            _note(log, _with_traceback("discover: aborted", exc))
            raise


def _discover_run(datadir: DataDir, log: RunLog) -> int:
    if not datadir.config_path.is_file():
        return _abort(log, "discover", "config.yaml is missing")
    if not datadir.search_db_path.is_file():
        return _abort(log, "discover", "search.db is missing; run careerhunter init")
    doc = load_config(datadir.config_path)
    try:
        conn = db.connect(datadir.search_db_path)
    except sqlite3.Error as exc:
        return _db_abort(log, "discover", exc)
    try:
        db.migrate(conn, "search")
        # A pass the watch agent started moments ago may still be running.
        recent = dt.timedelta(seconds=discover.PASS_SECONDS)
        if discovery_enabled(doc) and not discover.claim_pass(conn, discover._utc_now(), recent):
            log.append(BUSY)
            return EXIT_OK
        exclusions: dict[str, Any] = {}
        if datadir.exclusions_path.is_file():
            exclusions = load_exclusions(datadir.exclusions_path)
        outcome = discover.run(
            conn,
            doc,
            today=config_today(doc).isoformat(),
            confirm=_confirmer(Deadline(discover.PASS_SECONDS)),
            exclusions=exclusions,
        )
    except (sqlite3.Error, db.MigrationError) as exc:
        _rollback(conn)
        return _db_abort(log, "discover", exc)
    finally:
        _rollback(conn)
        conn.close()
    if outcome.notice:
        log.append(outcome.notice)
    for note in outcome.notes:
        log.append(note)
    log.note(f"discover: {len(outcome.rows)} queued")
    for row in outcome.rows:
        print(discover.format_row(row))
    _record_discover_success(datadir)
    return EXIT_OK


def _discover_by_hand(datadir: DataDir, action: str, candidate_id: int | None) -> int:
    """List, approve or reject. Interactive: no log file."""
    if not datadir.config_path.is_file():
        _err("error: config.yaml is missing")
        return EXIT_ERROR
    if not datadir.search_db_path.is_file():
        _err("error: search.db is missing; run careerhunter init")
        return EXIT_ERROR
    conn = db.connect(datadir.search_db_path)
    try:
        db.migrate(conn, "search")
        try:
            rows = _discover_rows(conn, action, candidate_id)
        except discover.DiscoverError as exc:
            _err(f"error: {exc}")
            return EXIT_ERROR
    finally:
        conn.close()
    for row in rows:
        print(discover.format_row(row))
    return EXIT_OK


def _discover_rows(
    conn: sqlite3.Connection, action: str, candidate_id: int | None
) -> Sequence[discover.Queued]:
    if action == "approve" and isinstance(candidate_id, int):
        return (discover.approve(conn, candidate_id),)
    if action == "reject" and isinstance(candidate_id, int):
        return (discover.reject(conn, candidate_id),)
    return discover.pending(conn)


def _abort(log: RunLog, command: str, reason: str) -> int:
    """An infrastructure stop: the reason to the log file, the usual error to stderr."""
    _note(log, f"{command}: aborted: {reason}")
    _err(f"error: {reason}")
    return EXIT_ERROR


def _db_abort(log: RunLog, command: str, exc: Exception) -> int:
    reason = f"{type(exc).__name__}: {exc}"
    _note(log, f"{command}: aborted: {reason}")
    _err(f"error: {reason}")
    return EXIT_ERROR


def _note(log: RunLog, text: str) -> None:
    """Write to the log file on the way out. If that fails too, say so on stderr."""
    try:
        log.note(text)
    except RunLogError as exc:
        _err(f"error: {exc}")


def _with_traceback(heading: str, exc: BaseException) -> str:
    return f"{heading}: traceback\n" + "".join(traceback.format_exception(exc))


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        with suppress(sqlite3.Error):
            conn.execute("ROLLBACK")


def cmd_resume(datadir: DataDir, args: argparse.Namespace) -> int:
    """Import a PDF or DOCX resume. Prints no content and no path."""
    if args.resume_action != "import":  # pragma: no cover - argparse limits the choices
        return EXIT_ERROR
    if not datadir.search_db_path.is_file():
        _err("error: search.db is missing; run careerhunter init")
        return EXIT_ERROR
    try:
        text = resume.extract_text(resume.read_file(args.file))
    except resume.ResumeError as exc:
        _err(f"error: {exc}; nothing was imported")
        return EXIT_ERROR
    conn = db.connect(datadir.search_db_path)
    try:
        db.migrate(conn, "search")
        imported = resume.import_text(datadir.root, conn, text)
    except resume.ResumeError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    finally:
        conn.close()
    print(f"imported resume: {imported.chars:,} characters")
    if not _resume_configured(datadir):
        print(f"set search.resume: {resume.CONFIG_VALUE} in config.yaml to use it")
    return EXIT_OK


def _resume_configured(datadir: DataDir) -> bool:
    """Whether config.yaml already names the imported file. Unreadable counts as no."""
    try:
        doc = load_config(datadir.config_path)
    except (ConfigError, OSError, yaml.YAMLError, ValueError):
        return False
    search = doc.get("search")
    value = search.get("resume") if isinstance(search, dict) else None
    return isinstance(value, str) and value.strip() == resume.CONFIG_VALUE


def cmd_embed_eval(datadir: DataDir, args: argparse.Namespace) -> int:
    """Rank stored cosines against tiers. The report stays in the data folder."""
    if not datadir.config_path.is_file():
        _err("error: config.yaml is missing")
        return EXIT_ERROR
    if not datadir.search_db_path.is_file():
        _err("error: search.db is missing")
        return EXIT_ERROR
    config = load_config(datadir.config_path)
    try:
        names = embed_eval.tier_names(config)
    except embed_eval.EvalError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    weight = embed_eval.embedding_weight(config)
    conn = db.connect_read_only(datadir.search_db_path)
    try:
        try:
            rows = embed_eval.load_rows(conn, names)
        except sqlite3.OperationalError:
            _err("error: search.db has no embedding_score column; run careerhunter migrate")
            return EXIT_ERROR
    finally:
        conn.close()
    text = embed_eval.render(rows, names, weight)
    path = embed_eval.write_report(datadir.root, text, dt.datetime.now(dt.UTC))
    print(path.relative_to(datadir.root).as_posix())
    return EXIT_OK


def cmd_scout_prompt(datadir: DataDir, args: argparse.Namespace) -> int:
    """Print a scout prompt from exclusions and hard gates. Writes nothing."""
    if not datadir.config_path.is_file():
        _err("error: config.yaml is missing")
        return EXIT_ERROR
    if not datadir.exclusions_path.is_file():
        _err("error: exclusions.yaml is missing")
        return EXIT_ERROR
    config = load_config(datadir.config_path)
    exclusions = load_exclusions(datadir.exclusions_path)
    sys.stdout.write(render(config, exclusions))
    return EXIT_OK


def _record_success(datadir: DataDir, name: str) -> None:
    """One UTC timestamp. Written only after a run has finished. 0600."""
    path = datadir.logs / name
    path.parent.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(stamp + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _record_watch_success(datadir: DataDir) -> None:
    """One UTC timestamp. Written only after a watch run has finished."""
    _record_success(datadir, "watch.success")


def _record_discover_success(datadir: DataDir) -> None:
    """One UTC timestamp. Written only after a discovery search exits 0."""
    _record_success(datadir, "discover.success")


# A watch run still unfinished this long after it started did not finish: it crashed,
# was killed, or stopped on an error. The budget plus five minutes for the slowest call.
_RUN_GRACE = dt.timedelta(seconds=watch.RUN_BUDGET + 300)


def _doctor_lanes(report: _Report, config_doc: dict[str, Any] | None) -> None:
    """Whether lane checking is on, so it is never off without saying so."""
    if config_doc is None:
        return
    configured = lanes.configured(config_doc)
    if configured is None:
        report.line(
            "lanes", "--", "not configured: lane checking is off, alerts go by fit then oldest"
        )
        return
    count = plural(len(configured), "lane", "lanes")
    minimum = off_lane_min_fit(config_doc)
    report.line("lanes", "OK", f"{count}; off-lane postings alert at {minimum} fit or better")


def _doctor_watch_runs(report: _Report, search: sqlite3.Connection, now: dt.datetime) -> None:
    """The last watch run: unfinished for too long is BAD; held counts from the last finish."""
    latest = search.execute("SELECT * FROM watch_runs ORDER BY id DESC LIMIT 1").fetchone()
    if latest is None:
        report.line("watch run", "--", "none recorded yet")
        return
    if latest["finished_at"] is None:
        started = dt.datetime.fromisoformat(str(latest["started_at"]))
        age = now - started
        minutes = max(0, int(age.total_seconds() // 60))
        if age > _RUN_GRACE:
            report.line(
                "watch run",
                "BAD",
                f"the last run started {minutes} min ago and did not finish; see logs/watch.log",
            )
        else:
            report.line("watch run", "OK", f"a run started {minutes} min ago is in progress")
    finished = search.execute(
        "SELECT * FROM watch_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if finished is None:
        report.line("watch held", "--", "no finished run yet, so nothing is counted")
        return
    detail = (
        f"{finished['held_unscored']} unscored, {finished['held_no_fit']} with no fit, "
        f"{finished['held_off_lane']} off-lane below the threshold"
    )
    if finished["budget_reached"]:
        detail += "; that run's budget left work for the next one"
    if finished["held_no_fit"]:
        report.line(
            "watch held",
            "WARN",
            f"{detail}. A tier name isn't in fit_rubric.gap_tiers; those wait until it is",
        )
    else:
        report.line("watch held", "OK", detail)


COMPONENT_NAMES = {"lm_studio": "LM Studio", "searxng": "SearXNG", "boards": "every board"}


def _ago(now: dt.datetime, stamp: str) -> str:
    minutes = max(0, int((now - dt.datetime.fromisoformat(stamp)).total_seconds() // 60))
    return f"{minutes} min ago"


def _unproductive_watch_runs(search: sqlite3.Connection, now: dt.datetime) -> int:
    """Watch runs in a row, newest first, that did no useful work (#42).

    A finished run with `useful` 0 counts, and so does a run unfinished past its grace:
    it crashed or was killed, so it did nothing useful. A run still in progress is not
    counted either way. The count stops at a useful run, and at a run finished before
    search migration 0018, whose usefulness is unknown.
    """
    count = 0
    for row in search.execute(
        "SELECT started_at, finished_at, useful FROM watch_runs ORDER BY id DESC"
    ):
        if row["finished_at"] is None:
            if now - dt.datetime.fromisoformat(str(row["started_at"])) > _RUN_GRACE:
                count += 1
            continue
        if row["useful"] != 0:
            break
        count += 1
    return count


def _doctor_usefulness(
    report: _Report,
    search: sqlite3.Connection,
    config_doc: dict[str, Any] | None,
    now: dt.datetime,
) -> None:
    """Did useful work, beside the ran stamps: a warning after N unproductive in a row."""
    limit = health_unproductive_runs(config_doc)
    last = search.execute("SELECT max(finished_at) FROM watch_runs WHERE useful = 1").fetchone()[0]
    when = f"last useful run {_ago(now, str(last))}" if last else "no useful run recorded yet"
    streak = _unproductive_watch_runs(search, now)
    if streak >= limit:
        report.line(
            "watch useful",
            "WARN",
            f"the last {streak} watch runs did no useful work; {when}. See logs/watch.log",
        )
    elif last:
        report.line("watch useful", "OK", when)
    else:
        report.line("watch useful", "--", when)
    if not discovery_enabled(config_doc):
        return
    row = search.execute("SELECT useful_at, unproductive FROM discovery_state").fetchone()
    useful_at, unproductive = (None, 0) if row is None else (row[0], int(row[1]))
    when = f"last useful pass {_ago(now, str(useful_at))}" if useful_at else "no useful pass yet"
    if unproductive >= limit:
        report.line(
            "discovery useful",
            "WARN",
            f"the last {unproductive} discovery passes got no usable reply; {when}",
        )
    elif useful_at:
        report.line("discovery useful", "OK", when)
    else:
        report.line("discovery useful", "--", when)


def _doctor_components(
    report: _Report, search: sqlite3.Connection, config_doc: dict[str, Any] | None
) -> None:
    """A component failing now: a warning at health.failure_runs runs in a row."""
    limit = health_failure_runs(config_doc)
    for health in search_store.component_health(search):
        if not health.failures:
            continue
        held = (
            ""
            if health.held is None
            else f"; {plural(health.held, 'posting', 'postings')} held for scoring"
        )
        report.line(
            "health",
            "WARN" if health.failures >= limit else "OK",
            f"{COMPONENT_NAMES[health.component]} failed the last "
            f"{plural(health.failures, 'run', 'runs')} ({health.last_failure}){held}",
        )


def _doctor_alert_hold(report: _Report, search: sqlite3.Connection) -> None:
    """Whether the bot is holding watcher alerts because it couldn't read config.yaml."""
    since = search_store.alerts_held_since(search)
    if since is None:
        report.line("bot alerts", "OK", "not held")
    else:
        report.line(
            "bot alerts",
            "BAD",
            f"held since {since}: the bot couldn't read config.yaml at its last poll",
        )


def _doctor_refusals(report: _Report, search: sqlite3.Connection) -> None:
    """Postings the bot gave up on after Telegram refused them too often (#42)."""
    given_up = search_store.given_up_count(search)
    if given_up:
        report.line(
            "bot refusals",
            "WARN",
            f"{plural(given_up, 'posting', 'postings')} refused by Telegram "
            f"{bot.REFUSALS_TO_GIVE_UP} times; given up and never sent (see logs/bot.log)",
        )
    else:
        report.line("bot refusals", "OK", "no posting given up")


def _doctor_resume(
    report: _Report,
    datadir: DataDir,
    config_doc: dict[str, Any] | None,
    search: sqlite3.Connection | None,
) -> None:
    """resume/: its modes, leftovers, the file against its recorded hash, and config."""
    folder = datadir.root / resume.RESUME_DIR
    configured = config_doc is not None and resume.configured(config_doc)
    if not folder.exists() and not folder.is_symlink():
        if configured:
            report.line("resume", "BAD", str(resume.ResumeError("not_imported")))
        else:
            report.line("resume", "--", "not imported (optional: careerhunter resume import)")
        return
    if folder.is_symlink() or not folder.is_dir():
        report.line("resume", "BAD", str(resume.ResumeError("folder")))
        return
    if stat.S_IMODE(folder.lstat().st_mode) != 0o700:
        os.chmod(folder, 0o700)
        report.line("resume/", "FIXED", "folder -> 0700")
    _doctor_private_folder(report, "resume/", folder)
    left = resume.leftovers(datadir.root)
    if left:
        report.line(
            "resume",
            "WARN",
            f"left by an interrupted or running import: {', '.join(left)}; "
            "the next import removes them",
        )
    if search is None:
        report.line("resume", "--", "hash not checked until search.db is readable")
        return
    try:
        resume.read_imported(datadir.root, search)
    except resume.ResumeError as exc:
        if exc.reason == "not_imported" and not configured:
            report.line("resume", "--", "not imported (optional: careerhunter resume import)")
        else:
            report.line("resume", "BAD", str(exc))
        return
    if config_doc is not None and not configured:
        report.line(
            "resume",
            "WARN",
            f"imported but not used: set search.resume: {resume.CONFIG_VALUE} in config.yaml",
        )
        return
    if config_doc is not None:
        try:
            resume.load(datadir.root, search, config_doc)
        except resume.ResumeError as exc:
            report.line("resume", "BAD", str(exc))
            return
    report.line("resume", "OK", "matches its import")


def cmd_doctor(datadir: DataDir, args: argparse.Namespace, *, clock: Clock | None = None) -> int:
    """Check everything and report. Reads only, apart from tightening file modes.

    Every age is measured against one reading of `clock` (UTC), so a test can fix it.
    """
    now = (clock or _utc_now)().astimezone(dt.UTC)
    report = _Report()
    report.line("data dir", "OK", f"{datadir.root} (volume {datadir.mount_point}, encrypted)")

    settings: ClaimSettings | None = None
    config_doc: dict[str, Any] | None = None
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
            config_doc = doc

    if settings is None:
        report.line("ei claim", "--", "unknown until config.yaml is valid")
        settings = ClaimSettings()
    elif settings.active:
        report.line("ei claim", "OK", f"{settings.active.label} active ({settings.active.period})")
    else:
        report.line("ei claim", "--", "none active (set ei.active_claim in config.yaml)")
    for warning in claims.warnings(settings, config_today(config_doc, now)):
        report.line("ei claim", "WARN", warning)
    if config_doc is not None:
        zone = timezone_name(config_doc)
        if zone is None:
            report.line(
                "timezone",
                "WARN",
                "schedule.timezone isn't set, so dates use UTC. Set yours in config.yaml, "
                "e.g. Europe/Berlin or UTC+05:30",
            )
        else:
            report.line("timezone", "OK", zone)

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
    _doctor_resume(report, datadir, config_doc, search)
    _doctor_lanes(report, config_doc)
    if search is not None:
        _doctor_watch_runs(report, search, now)
        _doctor_usefulness(report, search, config_doc, now)
        _doctor_components(report, search, config_doc)
        _doctor_alert_hold(report, search)
        _doctor_refusals(report, search)

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
                name = "orphan" if gap.kind == "orphan" else "reconcile"
                report.line(name, "BAD", f"{gap.correlation_id}: {gap.problem}")
            if gaps:
                # Reported only: doctor never repairs either layer (#34). Re-running
                # seed repairs seed rows; anything else is the operator's to decide.
                report.line("reconcile", "--", "left as they are; doctor never fixes either file")
            else:
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
    if (datadir.root / "reports").exists():
        _doctor_private_folder(report, "reports/", datadir.root / "reports")
    _doctor_private_folder(report, "snapshots/", datadir.snapshots)
    _doctor_private_folder(report, "logs/", datadir.logs)
    _doctor_bot(report, datadir, config_doc, now)
    _doctor_watch(report, datadir, config_doc, now)
    _doctor_discover(report, datadir, config_doc, now)

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
    p = sub.add_parser(
        "log", help="record a self-sourced application: the posting and the application at once"
    )
    p.add_argument("url", help="the job ad's link")
    p.add_argument("--method", required=True, help="how you applied, e.g. company_site or email")
    p.add_argument("--date", required=True, help="YYYY-MM-DD, today or yesterday")
    p.add_argument("--company", help="the employer, when the page doesn't say (with --title)")
    p.add_argument("--title", help="the role, when the page doesn't say (with --company)")
    p = sub.add_parser(
        "report",
        help="join one claim's EI record to search.db, read-only, into a private report",
    )
    p.add_argument("--claim", required=True, metavar="LABEL", help="the claim, e.g. 2026-27")
    p = sub.add_parser("secret", help="store or check a secret in the macOS Keychain")
    p.add_argument("action", choices=("set", "verify"))
    p.add_argument("name", choices=sorted(secrets_store.ALLOWED_NAMES))
    p = sub.add_parser("claim", help="manage EI claim records")
    claim_sub = p.add_subparsers(dest="claim_action", required=True)
    p = claim_sub.add_parser("seal", help="permanently close an earlier claim's record")
    p.add_argument("label", help="the claim to seal, e.g. 2025-26")
    p = sub.add_parser("bot", help="the Telegram bot")
    p.add_argument(
        "bot_action",
        choices=("run", "discover"),
        help="run: poll Telegram until stopped; discover: list chats that messaged the bot",
    )
    p = sub.add_parser(
        "service",
        help="run the bot, the watch, and optional discovery as launchd services on this Mac",
    )
    p.add_argument("service_action", choices=("install", "uninstall", "status"))
    sub.add_parser(
        "watch",
        help="fetch Greenhouse, Lever and Ashby boards on the watchlist, "
        "and approved discovery boards, once and exit",
    )
    discover_parser = sub.add_parser(
        "discover",
        help="queue companies from a local SearXNG; pages are not fetched",
    )
    discover_parser.add_argument(
        "discover_action",
        nargs="?",
        choices=("run", "list", "approve", "reject"),
        default="run",
        help="run: search and queue new rows; list: print pending rows; "
        "approve or reject: decide one pending row",
    )
    discover_parser.add_argument(
        "candidate_id",
        nargs="?",
        type=int,
        help="the pending row id for approve or reject",
    )
    sub.add_parser(
        "scout-prompt",
        help="print a scout prompt from exclusions and the hard gates the watcher skips on",
    )
    p = sub.add_parser("resume", help="import a resume for scoring; the text stays local")
    resume_sub = p.add_subparsers(dest="resume_action", required=True)
    p = resume_sub.add_parser(
        "import",
        help="read a PDF or DOCX locally and store its text in the data folder",
    )
    p.add_argument("file", type=Path, help="the PDF or DOCX file to import")
    sub.add_parser(
        "embed-eval",
        help="rank stored cosines against LLM tiers; the report stays in the data folder",
    )
    sub.add_parser("doctor", help="check the setup and report what is missing")
    sub.add_parser(
        "demo",
        help="run the pipeline on synthetic data in memory; reads and writes no files",
    )
    return parser


COMMANDS: dict[str, Callable[[DataDir, argparse.Namespace], int]] = {
    "init": cmd_init,
    "migrate": cmd_migrate,
    "seed": cmd_seed,
    "export": cmd_export,
    "log": cmd_log,
    "report": cmd_report,
    "claim": cmd_claim,
    "secret": cmd_secret,
    "bot": cmd_bot,
    "service": cmd_service,
    "watch": cmd_watch,
    "discover": cmd_discover,
    "scout-prompt": cmd_scout_prompt,
    "resume": cmd_resume,
    "embed-eval": cmd_embed_eval,
    "doctor": cmd_doctor,
}


def main(
    argv: Sequence[str] | None = None,
    probe: SystemProbe | None = None,
    *,
    clock: Clock | None = None,
) -> int:
    """Run one command. `clock` is the time doctor measures ages against (tests fix it)."""
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        # No data folder is read or written, so there is nothing for the guard to check.
        return demo.run()
    probe = probe or SystemProbe()
    try:
        datadir = storage_guard.check(require_exists=args.command != "init", probe=probe)
    except StorageGuardError as exc:
        _err(f"REFUSING TO START: {exc}")
        return EXIT_GUARD
    _warn_filevault(probe)
    try:
        if args.command == "doctor":
            return cmd_doctor(datadir, args, clock=clock)
        return COMMANDS[args.command](datadir, args)
    except (
        SeedError,
        ClaimError,
        PartialWriteError,
        BotStartupError,
        service.ServiceError,
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
