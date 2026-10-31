"""EI claims: config validation, the per-precision claim check, claim files and sealing."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from typing import Any

import pytest

from careerhunter import claims, db
from careerhunter.claims import Claim, ClaimError, ClaimSettings, Verdict, check_dating
from careerhunter.models import Dating
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, ACTIVE_ONLY, EARLIER

ENDED = Claim("2026-27", dt.date(2026, 8, 5), dt.date(2027, 8, 4))  # both Wednesdays
TODAY = dt.date(2026, 10, 31)


def section(active: str | None, *entries: dict[str, Any]) -> dict[str, Any]:
    return {"active_claim": active, "claims": list(entries)}


def entry(label: str, start: str, end: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"label": label, "start": dt.date.fromisoformat(start)}
    if end:
        e["end"] = dt.date.fromisoformat(end)
    return e


# --- Config ------------------------------------------------------------------------------


def test_valid_sections() -> None:
    assert claims.validate_section(None) == []
    assert claims.validate_section(section(None)) == []
    assert claims.validate_section(section("2026-27", entry("2026-27", "2026-08-05"))) == []
    assert (
        claims.validate_section(
            section(
                "2026-27",
                entry("2025-26", "2025-06-04", "2026-06-02"),
                entry("2026-27", "2026-08-05", "2027-08-04"),
            )
        )
        == []
    )
    assert claims.validate_section({"claims": None}) == []


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ("2026-27", "ei must be a mapping"),
        ({"active": "2026-27"}, "unknown keys"),
        ({"claims": "2026-27"}, "ei.claims must be a list"),
        (section(None, {"label": "2026-27", "start": "2026-08-05", "x": 1}), "unknown keys"),
        (section(None, entry("2026-28", "2026-08-05", "2027-01-01")), "consecutive years"),
        (section(None, entry("2026_27", "2026-08-05", "2027-01-01")), "not a claim label"),
        (section(None, {"label": "../x", "start": "2026-08-05"}), "not a claim label"),
        (section(None, {"label": 2026, "start": "2026-08-05"}), "not a claim label"),
        (section("2026-27", {"label": "2026-27"}), "start must be a YYYY-MM-DD date"),
        (section("2026-27", {"label": "2026-27", "start": "soon"}), "start must be"),
        (
            section("2026-27", {"label": "2026-27", "start": "2026-08-05", "end": "x"}),
            "end must be",
        ),
        (section("2026-27", entry("2026-27", "2026-08-05", "2026-08-01")), "is before start"),
        (section("2026-27", entry("2026-27", "2027-01-04")), "must start in 2026"),
        (
            section("2026-27", entry("2026-27", "2026-08-05"), entry("2026-27", "2026-09-01")),
            "more than once",
        ),
        (
            section(
                "2026-27",
                entry("2025-26", "2025-06-04", "2026-08-10"),
                entry("2026-27", "2026-08-05"),
            ),
            "overlap",
        ),
        (section("2027-28", entry("2026-27", "2026-08-05", "2027-08-04")), "is not one of"),
        (
            section(None, entry("2026-27", "2026-08-05")),
            "not the active claim, so it needs an end date",
        ),
        (
            section(
                "2025-26",
                entry("2025-26", "2025-06-04", "2026-06-02"),
                entry("2026-27", "2026-08-05"),
            ),
            "2026-27 is not the active claim",
        ),
    ],
)
def test_section_problems(doc: Any, message: str) -> None:
    assert any(message in p for p in claims.validate_section(doc)), claims.validate_section(doc)


def test_settings_from_config() -> None:
    doc = {
        "ei": section(
            "2026-27", entry("2025-26", "2025-06-04", "2026-06-02"), entry("2026-27", "2026-08-05")
        )
    }
    settings = claims.settings_from_config(doc)
    assert settings.active == ACTIVE
    assert settings.get("2025-26") == EARLIER
    assert settings.get("2024-25") is None
    assert claims.settings_from_config({}) == ClaimSettings()


def test_ended_active_claim_warns() -> None:
    assert claims.warnings(ClaimSettings(ENDED, (ENDED,)), dt.date(2027, 8, 5))
    assert claims.warnings(ClaimSettings(ENDED, (ENDED,)), dt.date(2027, 8, 4)) == []
    assert claims.warnings(ACTIVE_ONLY, TODAY) == []


def test_keep_until() -> None:
    assert claims.keep_until(dt.date(2026, 6, 2)) == dt.date(2032, 6, 2)
    assert claims.keep_until(dt.date(2028, 2, 29)) == dt.date(2034, 2, 28)


# --- The claim check, one rule per precision -------------------------------------------


def exact(day: str) -> Dating:
    return Dating("exact", day=day)


def week(monday: str) -> Dating:
    return Dating("week", week_of=monday)


def test_exact_date_must_fall_within_the_claim() -> None:
    assert check_dating(ENDED, exact("2026-08-05"))[0] is Verdict.ACCEPT  # first day
    assert check_dating(ENDED, exact("2027-08-04"))[0] is Verdict.ACCEPT  # last day
    verdict, message = check_dating(ENDED, exact("2026-08-04"))
    assert verdict is Verdict.REFUSE
    assert message == "2026-08-04 is before claim 2026-27 started (2026-08-05)"
    verdict, message = check_dating(ENDED, exact("2027-08-05"))
    assert verdict is Verdict.REFUSE
    assert "after claim 2026-27 ended (2027-08-04)" in message


def test_exact_date_with_open_ended_claim() -> None:
    assert check_dating(ACTIVE, exact("2031-01-01"))[0] is Verdict.ACCEPT


def test_week_overlapping_the_claim_is_accepted() -> None:
    assert check_dating(ENDED, week("2026-08-03"))[0] is Verdict.ACCEPT  # straddles the start
    assert check_dating(ENDED, week("2027-08-02"))[0] is Verdict.ACCEPT  # straddles the end


def test_week_entirely_outside_the_claim_is_refused() -> None:
    verdict, message = check_dating(ENDED, week("2026-07-27"))  # Sunday 08-02 is before
    assert verdict is Verdict.REFUSE
    assert "entirely before claim 2026-27 started" in message
    verdict, message = check_dating(ENDED, week("2027-08-09"))
    assert verdict is Verdict.REFUSE
    assert "entirely after claim 2026-27 ended" in message


def test_unknown_date_is_accepted_with_a_warning() -> None:
    verdict, message = check_dating(ENDED, Dating("unknown"))
    assert verdict is Verdict.WARN
    assert '"Date unknown"' in message


# --- Claim files ---------------------------------------------------------------------------


def test_active_file_is_created_complete(datadir: DataDir) -> None:
    active = claims.open_active(datadir, ACTIVE_ONLY)
    assert active is not None
    try:
        assert active.created
        assert active.path == datadir.root / "ei_record_2026-27.db"
        assert claims.read_file_claim(active.conn) == claims.FileClaim(
            "2026-27", "2026-08-05", None, None
        )
        assert active.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        active.conn.close()
    assert not (datadir.root / "ei_record_2026-27.db.tmp").exists()
    for f in db.db_files(active.path):
        if f.exists():
            assert f.stat().st_mode & 0o777 == 0o600
    again = claims.open_active(datadir, ACTIVE_ONLY)
    assert again is not None and not again.created
    again.conn.close()


def test_no_active_claim_creates_no_file(datadir: DataDir) -> None:
    assert claims.open_active(datadir, ClaimSettings()) is None
    assert datadir.ei_record_files() == []


def test_a_leftover_partial_file_is_discarded(datadir: DataDir) -> None:
    """A crash while building leaves only the .tmp file, which never held a record."""
    (datadir.root / "ei_record_2026-27.db.tmp").write_bytes(b"half-built")
    with claims.active_claim(datadir, ACTIVE_ONLY) as active:
        assert active is not None and active.created
    assert not (datadir.root / "ei_record_2026-27.db.tmp").exists()


def test_end_date_from_config_is_recorded(datadir: DataDir) -> None:
    claims.open_active(datadir, ACTIVE_ONLY).conn.close()  # type: ignore[union-attr]
    with claims.active_claim(datadir, ClaimSettings(ENDED, (ENDED,))) as active:
        assert active is not None
        assert active.messages == ["claim 2026-27: end date recorded as 2027-08-04"]
        assert claims.read_file_claim(active.conn).end == "2027-08-04"  # type: ignore[union-attr]
    with pytest.raises(ClaimError, match="Add end back"):
        claims.open_active(datadir, ACTIVE_ONLY)


def test_a_file_that_does_not_match_config_is_refused(datadir: DataDir) -> None:
    claims.open_active(datadir, ACTIVE_ONLY).conn.close()  # type: ignore[union-attr]
    moved = Claim("2026-27", dt.date(2026, 9, 1))
    with pytest.raises(ClaimError, match="start date can't change"):
        claims.open_active(datadir, ClaimSettings(moved, (moved,)))


def test_write_blockers(datadir: DataDir, opener: Any) -> None:
    both = ClaimSettings(ACTIVE, (EARLIER, ACTIVE))
    assert claims.write_blockers(datadir, both) == []
    _make_earlier_file(datadir)
    assert claims.write_blockers(datadir, both) == [
        "claim 2025-26 is not the active claim, but its record is not sealed yet. "
        "Run: careerhunter claim seal 2025-26"
    ]
    with pytest.raises(ClaimError, match="claim seal 2025-26"):
        claims.require_write_ready(datadir, both)
    assert "not listed under ei.claims" in claims.write_blockers(datadir, ACTIVE_ONLY)[0]
    (datadir.root / "ei_record_notes.db").write_bytes(b"")
    assert "not a valid claim file name" in claims.write_blockers(datadir, both)[-1]


def _make_earlier_file(datadir: DataDir) -> None:
    """A record for EARLIER, as it would be while EARLIER was the active claim."""
    settings = ClaimSettings(EARLIER, (EARLIER,))
    with claims.active_claim(datadir, settings) as earlier:
        assert earlier is not None
        earlier.conn.execute(
            "INSERT INTO activities (date_precision, date, type, description) "
            "VALUES ('exact', '2026-01-15', 'course', 'Synthetic course')"
        )


# --- Sealing -----------------------------------------------------------------------------

BOTH = ClaimSettings(ACTIVE, (EARLIER, ACTIVE))


def test_seal_plan_describes_and_changes_nothing(datadir: DataDir) -> None:
    _make_earlier_file(datadir)
    path = datadir.ei_record_path("2025-26")
    before = path.read_bytes()
    plan = claims.plan_seal(datadir, BOTH, "2025-26", TODAY)
    text = plan.describe()
    for expected in ("2025-26", "2025-06-04 to 2026-06-02", "read-only (0400)", "2032-06-02"):
        assert expected in text
    assert path.read_bytes() == before
    assert claims.read_claim_file(path).sealed_at is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("label", "settings", "message"),
    [
        ("2026-27", BOTH, "is the active claim"),
        ("2024-25", BOTH, "not listed"),
        ("2024-26", BOTH, "consecutive years"),
        (
            "2025-26",
            ClaimSettings(None, (Claim("2025-26", EARLIER.start, dt.date(2026, 11, 1)),)),
            "hasn't happened yet",
        ),
    ],
)
def test_seal_is_refused(
    datadir: DataDir, label: str, settings: ClaimSettings, message: str
) -> None:
    _make_earlier_file(datadir)
    with pytest.raises(ClaimError, match=message):
        claims.plan_seal(datadir, settings, label, TODAY)
    assert claims.read_claim_file(datadir.ei_record_path("2025-26")).sealed_at is None  # type: ignore[union-attr]


def test_seal_needs_the_file(datadir: DataDir) -> None:
    with pytest.raises(ClaimError, match="no record file"):
        claims.plan_seal(datadir, BOTH, "2025-26", TODAY)


def test_seal(datadir: DataDir, opener: Any) -> None:
    _make_earlier_file(datadir)
    path = datadir.ei_record_path("2025-26")
    claims.seal(claims.plan_seal(datadir, BOTH, "2025-26", TODAY))

    assert path.stat().st_mode & 0o777 == 0o400
    assert [f for f in db.db_files(path)[1:] if f.exists()] == []
    ro = opener(path, read_only=True)
    assert ro.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    file_claim = claims.read_file_claim(ro)
    assert file_claim is not None and file_claim.sealed_at and file_claim.end == "2026-06-02"
    assert ro.execute("SELECT count(*) FROM activities").fetchone()[0] == 1  # still readable
    assert claims.write_blockers(datadir, BOTH) == []
    with pytest.raises(ClaimError, match="already sealed"):
        claims.plan_seal(datadir, BOTH, "2025-26", TODAY)


def test_writes_to_an_earlier_claim_are_refused_at_every_layer(datadir: DataDir) -> None:
    _make_earlier_file(datadir)
    path = datadir.ei_record_path("2025-26")
    claims.seal(claims.plan_seal(datadir, BOTH, "2025-26", TODAY))
    # The app: only the active claim is ever opened for writing.
    with claims.active_claim(datadir, BOTH) as active:
        assert active is not None and active.claim.label == "2026-27"
    # Config pointing back at a sealed claim is refused too.
    with pytest.raises(ClaimError, match="was sealed"):
        claims.open_active(datadir, ClaimSettings(EARLIER, (EARLIER,)))
    # The file system: the file is read-only.
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.connect(path)
    # The database: even with write permission back, the seal triggers refuse.
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute(
                "INSERT INTO activities (date_precision, type, description) "
                "VALUES ('unknown', 'course', 'late')"
            )
    finally:
        conn.close()
