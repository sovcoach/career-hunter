"""The job description of every posting the bot may alert is kept (Batch R, search
migration 0036), the alert's JD button sends it, and an Applied tap uses it.
Synthetic postings only."""

from __future__ import annotations

import copy
import datetime as dt
import stat

from careerhunter import bot as botmod
from careerhunter import db, jobdesc, search_store, snapshots, watch
from careerhunter.filters import PostingText
from careerhunter.score import Score
from careerhunter.storage_guard import DataDir
from tests import test_bot
from tests.test_bot import Env, alerted
from tests.test_bot_jd import jd_rows
from tests.test_score import RUBRIC
from tests.test_watch import Scripted, company, fetched, url

env = test_bot.env

DUTIES = "Plan synthetic deliveries.\n\nKeep the synthetic dock tidy. Requisition 123456."


# --- normalizing ----------------------------------------------------------------------


def test_the_normalized_text_ignores_case_spacing_and_long_numbers() -> None:
    assert jobdesc.normalized("  Plan  THE\nDock. Req 123456, 2026-11-02 ") == (
        "plan the dock. req , -11-02"
    )
    a = jobdesc.normalized_sha256("Plan the dock. Requisition 123456.")
    b = jobdesc.normalized_sha256("plan  the DOCK.\nRequisition 987654.")
    assert a == b
    assert a != jobdesc.normalized_sha256("Plan the yard. Requisition 123456.")
    assert jobdesc.normalized_sha256("Shift 12 of 30") != jobdesc.normalized_sha256(
        "Shift 13 of 30"
    )


# --- the watcher keeps it --------------------------------------------------------------


def _run(datadir: DataDir, tier: str, description: str = DUTIES, ident: int = 1001) -> list[str]:
    (datadir.root / "snapshots").mkdir(mode=0o700, exist_ok=True)
    conn = db.connect(datadir.search_db_path)
    db.migrate(conn, "search")

    def scorer(posting: PostingText) -> Score:
        return Score(llm_output="model text", gaps="[]", tier=tier)

    job = {
        "id": ident,
        "title": "Operations Coordinator",
        "absolute_url": f"https://boards.greenhouse.io/examplerobotics/jobs/{ident}",
        "location": {"name": ""},  # unknown: never dropped, scored as usual
        "content": description,
    }
    try:
        return watch.run(
            conn,
            [company("Example Robotics", "greenhouse", "examplerobotics")],
            today="2026-11-02",
            fetcher=Scripted(
                {url("greenhouse", "examplerobotics"): fetched({"jobs": [job]})}, on_board=True
            ),
            config=copy.deepcopy(RUBRIC),
            scorer=scorer,
            data_dir=datadir.root,
        )
    finally:
        conn.close()


def _kept(datadir: DataDir) -> tuple[str | None, str | None]:
    conn = db.connect_read_only(datadir.search_db_path)
    try:
        row = conn.execute("SELECT snapshot_path, jd_sha256 FROM postings").fetchone()
        return row[0], row[1]
    finally:
        conn.close()


def test_an_alertable_posting_keeps_the_description_it_was_scored_on(datadir: DataDir) -> None:
    _run(datadir, "high")
    path, digest = _kept(datadir)
    assert path is not None and path.startswith("snapshots/2026/") and path.endswith(".txt")
    kept = datadir.root / path
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert "Plan synthetic deliveries." in kept.read_text(encoding="utf-8")
    assert digest == jobdesc.normalized_sha256(kept.read_text(encoding="utf-8"))


def test_a_low_fit_keeps_no_description(datadir: DataDir) -> None:
    _run(datadir, "low")
    assert _kept(datadir) == (None, None)
    assert list((datadir.root / "snapshots").rglob("*.txt")) == []


def test_the_same_text_is_not_kept_twice(datadir: DataDir) -> None:
    _run(datadir, "high")
    first = _kept(datadir)
    state = watch._State(
        lines=[], hook=lambda *a: None, budget=None, now=lambda: dt.datetime.now(dt.UTC)
    )
    state.data_dir = datadir.root
    state.today = "2026-11-03"

    def text(description: str) -> PostingText:
        return PostingText(
            company="Example Robotics",
            title="Operations Coordinator",
            location="",
            description=description,
        )

    conn = db.connect(datadir.search_db_path)
    try:
        same = text(DUTIES.replace("123456", "999999"))
        watch._keep_jd(conn, state, 1, same, "Example Robotics", "greenhouse", "'x'")
        assert _kept(datadir) == first  # the same after normalizing: nothing new
        other = text("Plan other deliveries.")
        watch._keep_jd(conn, state, 1, other, "Example Robotics", "greenhouse", "'x'")
    finally:
        conn.close()
    second = _kept(datadir)
    assert second[0] != first[0] and second[1] != first[1]
    assert len(list((datadir.root / "snapshots").rglob("*.txt"))) == 2


def test_a_posting_with_a_saved_job_ad_keeps_its_ad(datadir: DataDir) -> None:
    (datadir.root / "snapshots").mkdir(mode=0o700, exist_ok=True)
    conn = db.connect(datadir.search_db_path)
    db.migrate(conn, "search")
    try:
        conn.execute("INSERT INTO companies (name, source) VALUES ('Example Robotics', 'manual')")
        conn.execute(
            "INSERT INTO postings (company_id, title, first_seen, source, snapshot_path) "
            "VALUES (1, 'Analyst', '2026-11-02', 'watcher', 'snapshots/2026/ad.html')"
        )
        search_store.keep_jd(conn, 1, "snapshots/2026/jd.txt", "0" * 64)
        assert search_store.kept_jd(conn, 1) is None
    finally:
        conn.close()


# --- the JD button ---------------------------------------------------------------------


def _keep(env: Env, posting_id: int, text: str) -> None:
    (env.datadir.root / "snapshots").mkdir(mode=0o700, exist_ok=True)
    path = snapshots.save(env.datadir.root, text.encode("utf-8"), "txt", dt.date(2026, 11, 2))
    conn = db.connect(env.datadir.search_db_path)
    try:
        search_store.keep_jd(conn, posting_id, path, jobdesc.normalized_sha256(text))
    finally:
        conn.close()


def test_the_jd_button_sends_the_kept_text_and_spends_nothing(env: Env) -> None:
    posting = alerted(env)
    _keep(env, posting, DUTIES)
    assert env.press("JD 1") == "callback:jd"
    assert env.api.texts()[-1] == (
        "Job description: Example Robotics — Synthetic Analyst\n\n" + DUTIES
    )
    assert env.press("JD 1") == "callback:jd"  # again: it is not spent
    env.press("Applied 1")
    assert env.api.texts()[-1] == "How did you apply?"


def test_the_jd_button_says_when_none_was_kept(env: Env) -> None:
    alerted(env)
    env.press("JD 1")
    assert env.api.texts()[-1] == botmod.JD_NONE


def test_a_long_jd_comes_in_at_most_four_messages() -> None:
    paragraph = "Synthetic duty sentence. " * 60  # about 1,500 characters
    parts = botmod._jd_messages("\n\n".join([paragraph.strip()] * 20))
    assert len(parts) == botmod.JD_MESSAGES
    assert all(len(part) <= botmod.JD_CHUNK for part in parts)
    assert parts[-1].endswith(botmod.JD_REST)
    assert not parts[0].endswith(" ")
    short = botmod._jd_messages("One paragraph.\n\nTwo.")
    assert short == ["One paragraph.\n\nTwo."]


def test_one_word_longer_than_a_message_is_cut_hard() -> None:
    parts = botmod._jd_messages("x" * (botmod.JD_CHUNK + 10))
    assert len(parts[0]) == botmod.JD_CHUNK
    assert parts[1] == "x" * 10


# --- Applied uses it -----------------------------------------------------------------


def test_an_applied_tap_records_the_kept_jd(env: Env) -> None:
    posting = alerted(env)
    _keep(env, posting, DUTIES)
    env.press("Applied 1")
    env.press("Email")
    env.press("Today")
    assert "Job description saved." in env.api.texts()[-1]
    (row,) = jd_rows(env)
    assert row["text"] == DUTIES
    assert row["jd_source"] == "fetched"


def test_an_alert_from_before_jds_were_kept_still_asks_for_one(env: Env) -> None:
    alerted(env)
    env.press("Applied 1")
    env.press("Email")
    env.press("Today")
    assert env.api.texts()[-1] == botmod.JD_REQUEST
    assert jd_rows(env) == []
