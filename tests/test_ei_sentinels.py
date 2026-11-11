"""No learning field reaches an EI file (#34, ADR-014).

A distinct value is planted in every field added to search.db since v0.3.0 and by
Batch E, then every path that writes the EI record runs: Applied from an alert, a
status update, /log (one step, and on a link already stored), careerhunter log, a
job-ad upload and an activity. The EI file and its WAL are then scanned, byte for
byte, for every planted value. Synthetic data only.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from careerhunter import db, search_store, selfsourced
from careerhunter.models import Dating
from tests import test_bot
from tests.test_bot import AD_URL, Env, ei_apps

env = test_bot.env  # the bot fixture, shared with test_bot

STORED_URL = "https://jobs.example.com/ads/stored"
JD_FILE = b"Synthetic planner duties at Example Robotics.\n"
DAY = "2026-10-21"

TEXT = {
    "schedule": "SENTINEL-schedule-e4",
    "lane": "SENTINEL-lane-e4",
    "key_requirements": "SENTINEL-requirement-e4",
    "scout_metadata": "SENTINEL-tier-e4",
    "gaps": "SENTINEL-gap-e4",
    "llm_output": "SENTINEL-llm-e4",
    "scored_resume_sha256": "5e" * 32,
    "resume_sha256": "e4" * 32,
    "ats_key": "greenhouse:sentinelboard:e4",
    "discovery_name": "SENTINEL-discovery-e4",
    "discovery_url": "https://boards.greenhouse.io/sentineldiscoverye4",
    "skip_reason": "SENTINEL-skip-reason-e4",
    "alerted_at": "2031-02-03T04:05:06Z",
    "score_attempted_at": "2031-02-03T04:05:07Z",
    "posted_date": "2031-03-04",
    "refused_at": "2031-02-03T04:05:08Z",
    "health_at": "2031-02-03T04:05:09Z",
    "useful_at": "2031-02-03T04:05:10Z",
    "run_started": "2031-02-03T04:05:11Z",
    "jd_sha256": "a5" * 32,
    "jd_path": "snapshots/2031/sentinel-jd-e4.txt",
}
COSINE = 0.7311073133
VECTOR = struct.pack("<4f", 0.11, 0.22, 0.33, 0.44)
RESUME_VECTOR = struct.pack("<4f", 0.55, 0.66, 0.77, 0.88)


def _plant(conn: sqlite3.Connection, posting_id: int, *, first: bool) -> None:
    """A value in every learning field since v0.3.0, and in each of Batch E's. The rows
    that are not per posting are planted once, with the first posting."""
    run = conn.execute(
        "INSERT INTO watch_runs (started_at, finished_at, boards_fetched, boards_failed, "
        "boards_left, held_unscored, held_no_fit, held_off_lane, useful) "
        "VALUES (?, ?, 7, 0, 0, 0, 0, 0, 1)",
        (TEXT["run_started"], TEXT["run_started"]),
    ).lastrowid
    conn.execute(
        "UPDATE postings SET schedule = ?, alerted_at = ?, embedding_score = ?, "
        "scored_resume_sha256 = ?, score_attempted_at = ?, fit = 'high', lane = ?, "
        "lane_checked = 1, key_requirements = ?, score_run_id = ?, posted_date = ?, "
        "ats_key = ?, gaps = ?, llm_output = ?, scout_metadata = ? WHERE id = ?",
        (
            TEXT["schedule"],
            TEXT["alerted_at"],
            COSINE,
            TEXT["scored_resume_sha256"],
            TEXT["score_attempted_at"],
            TEXT["lane"],
            json.dumps([TEXT["key_requirements"]]),
            run,
            TEXT["posted_date"],
            TEXT["ats_key"] + ("" if first else ":2"),
            json.dumps([{"name": TEXT["gaps"]}]),
            TEXT["llm_output"],
            json.dumps({"tier": TEXT["scout_metadata"]}),
            posting_id,
        ),
    )
    conn.execute(
        "INSERT INTO posting_embeddings (posting_id, dim, vector, embedded_at) VALUES (?, 4, ?, ?)",
        (posting_id, VECTOR, TEXT["useful_at"]),
    )
    if not first:
        return
    conn.execute(
        "INSERT OR REPLACE INTO resume_embedding (id, content_sha256, dim, vector, embedded_at) "
        "VALUES (1, ?, 4, ?, ?)",
        (TEXT["resume_sha256"], RESUME_VECTOR, TEXT["useful_at"]),
    )
    conn.execute(
        "INSERT INTO discovery_candidates (kind, url, ats_type, ats_slug, suggested_name, "
        "status, first_seen) VALUES ('board', ?, 'greenhouse', 'sentineldiscoverye4', ?, "
        "'pending', ?)",
        (TEXT["discovery_url"], TEXT["discovery_name"], DAY),
    )
    conn.execute(
        "INSERT INTO skips (posting_id, date, reason, by_operator) VALUES (?, ?, ?, 1)",
        (posting_id, DAY, TEXT["skip_reason"]),
    )
    conn.execute(
        "INSERT INTO alert_refusals (posting_id, refusals, last_refused_at, given_up_at) "
        "VALUES (?, 3, ?, ?)",
        (posting_id, TEXT["refused_at"], TEXT["refused_at"]),
    )
    conn.execute(
        "UPDATE component_health SET failures = 7, last_failure = 'bad-reply', held = 7, "
        "updated_at = ?, alerted_failures = 7",
        (TEXT["health_at"],),
    )
    conn.execute("UPDATE discovery_state SET useful_at = ?, unproductive = 7", (TEXT["useful_at"],))


def _stored(env: Env, url: str, title: str, *, first: bool = True) -> int:
    conn = db.connect(env.datadir.search_db_path)
    try:
        db.migrate(conn, "search")
        with db.transaction(conn):
            _, posting_id, _, _ = search_store.find_or_create_posting(
                conn,
                test_bot.Posting("Example Robotics", title, DAY, url=url, source="watcher"),
                Dating("exact", day=DAY),
            )
            _plant(conn, posting_id, first=first)
        return posting_id
    finally:
        conn.close()


def _plant_jd(env: Env) -> None:
    """search migration 0020's JD fields. EI keeps its own JD rows; these never go there."""
    conn = db.connect(env.datadir.search_db_path)
    try:
        conn.execute(
            "UPDATE applications SET jd_sha256 = ?, jd_path = ?",
            (TEXT["jd_sha256"], TEXT["jd_path"]),
        )
    finally:
        conn.close()


def _ei_bytes(env: Env) -> bytes:
    path = env.datadir.ei_record_path(test_bot.ACTIVE.label)
    return b"".join(part.read_bytes() for part in db.db_files(path) if part.exists())


def _forms(value: str) -> list[bytes]:
    return [value.encode(), value.encode("utf-16-le")]


def test_no_learning_field_reaches_an_ei_file(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posting_id = _stored(env, AD_URL, "Synthetic Analyst")
    _stored(env, STORED_URL, "Synthetic Planner", first=False)
    env.web.routes[STORED_URL] = test_bot.SHELL
    # Applied from an alert: the stored posting, with every field planted.
    env.bot._finish_apply(posting_id, "email", Dating("exact", day=DAY))
    _plant_jd(env)  # search.db's JD reference fields, on every application so far
    env.say("/status a1 screening")
    # /log in one step on a link already stored, then the JD file it asks for.
    env.say(f"/log {STORED_URL} | referral | {DAY} | Example Robotics | Synthetic Planner")
    env.api.files["documents/f1.bin"] = JD_FILE
    env.say(reply_to=env.prompt_id(), document={"file_id": "f1", "file_size": len(JD_FILE)})
    env.say("/activity networking 2026-10-22 Synthetic meetup")
    # careerhunter log goes through the same selfsourced.record.
    with env.bot._writable(env.bot._config()) as (search, active):
        selfsourced.record(
            env.datadir.root,
            search,
            active,
            selfsourced.describe(
                AD_URL, env.web, test_bot.NOW.date(), company="Example Robotics", title="Analyst"
            ),
            "company_site",
            Dating("exact", day="2026-10-23"),
            test_bot.NOW.date(),
            env.web,
        )
    assert len(ei_apps(env)) == 3  # every path wrote the EI record
    ei = env.ei()
    assert ei is not None
    try:
        statuses = [r[0] for r in ei.execute("SELECT status FROM status_history ORDER BY id")]
        assert statuses == ["applied", "screening", "applied", "applied"]
        assert ei.execute("SELECT count(*) FROM activities").fetchone()[0] == 1
        assert ei.execute("SELECT count(*) FROM edits").fetchone()[0] == 1  # the JD file
        assert ei.execute("SELECT count(*) FROM job_descriptions").fetchone()[0] == 1
    finally:
        ei.close()
    found = _ei_bytes(env)
    for name, value in TEXT.items():
        for form in _forms(value):
            assert form not in found, name
    for name, blob in (
        ("embedding_score", struct.pack("<d", COSINE)),
        ("embedding_score (big-endian)", struct.pack(">d", COSINE)),
        ("posting vector", VECTOR),
        ("resume vector", RESUME_VECTOR),
    ):
        assert blob not in found, name


def test_the_ei_schema_has_no_learning_column(env: Env) -> None:
    """The schema half: no column for any of those fields exists in an EI file."""
    _stored(env, AD_URL, "Synthetic Analyst")
    env.say(f"/log {AD_URL} | email | {DAY}")
    path = env.datadir.ei_record_path(test_bot.ACTIVE.label)
    conn = db.connect_read_only(path)
    try:
        columns = {
            row[1]
            for table in ("applications", "status_history", "activities", "edits", "claim")
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    for learning in (
        "schedule",
        "alerted_at",
        "embedding_score",
        "scored_resume_sha256",
        "fit",
        "lane",
        "key_requirements",
        "score_run_id",
        "posted_date",
        "ats_key",
        "by_operator",
        "gaps",
        "llm_output",
        "notes",
        "jd_sha256",
        "jd_path",
    ):
        assert learning not in columns
    for table in (
        "posting_embeddings",
        "resume_embedding",
        "discovery_candidates",
        "discovery_state",
        "watch_runs",
        "skips",
        "alert_refusals",
        "component_health",
    ):
        assert table not in tables
    assert Path(path).exists()
