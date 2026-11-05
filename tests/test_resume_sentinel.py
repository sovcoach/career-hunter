"""The resume reaches the local model and nowhere else (#37).

A made-up sentinel phrase is put in a synthetic resume, imported as a PDF and as a
DOCX, and followed through the real commands: `resume import`, `watch` (with fake
board, model and embedding transports) and a bot poll that sends the alerts. The
sentinel must be in the scoring request, and absent from stdout, stderr, watch.log,
bot.log, every log record, every message the bot sends, every search.db cell, and the
text, repr and traceback of the exceptions forced on the import and scoring paths.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from pathlib import Path

import pytest
import yaml

from careerhunter import bot as botmod
from careerhunter import cli, db, embed, resume
from careerhunter.fetch import Fetched
from careerhunter.filters import PostingText
from careerhunter.platform_checks import SystemProbe
from careerhunter.score import ScoreError, score_posting
from careerhunter.storage_guard import DataDir
from tests.conftest import ACTIVE, set_claims
from tests.test_bot import CHAT, TOKEN, Clock, FakeTelegram
from tests.test_resume import document_xml, make_docx, make_pdf
from tests.test_watch import fetched, one_greenhouse, url

SENTINEL = "Zq7 Kelvin Marigold Orbit Sentinel Phrase 0042 Lantern"
# Long enough that a copy of it in a gap name is caught by D-A1's 40-character check.
assert len(SENTINEL) >= 40

RESUME_LINE = f"Avery Example {SENTINEL}"


def _forms(text: str) -> list[str]:
    """The sentinel as text, as UTF-8 hex (a BLOB in a dump), and as JSON would escape it."""
    return [text, text.encode().hex(), text.encode().hex().upper(), json.dumps(text)[1:-1]]


def assert_absent(where: str, haystack: str) -> None:
    for form in _forms(SENTINEL):
        assert form not in haystack, f"the sentinel reached {where}"


@pytest.fixture
def inited(data_env: Path, probe: SystemProbe, capsys: pytest.CaptureFixture[str]) -> Path:
    assert cli.main(["init"], probe=probe) == 0
    set_claims(data_env, ACTIVE.label, ACTIVE)
    path = data_env / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["search"]["resume"] = resume.CONFIG_VALUE
    doc["search"]["location_rules"]["remote"] = ["Example Region"]
    doc["telegram"] = {"chat_id": CHAT}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    capsys.readouterr()
    return data_env


def _boards(board: str, **_kw: object) -> Fetched:
    if board == url("greenhouse", "examplerobotics"):
        return fetched(one_greenhouse("examplerobotics"))
    if board == url("lever", "samplelogistics"):
        return fetched(
            [
                {
                    "id": "abc-123",
                    "text": "Operations Coordinator",
                    "hostedUrl": "https://jobs.lever.co/samplelogistics/abc-123",
                    "categories": {"location": "Example Region"},
                    "workplaceType": "remote",
                }
            ]
        )
    raise AssertionError(board)


class FakeModel:
    """The loopback scoring server. One reply quotes the resume outside the JSON; the
    other copies it into a gap name, which D-A1 must refuse."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def __call__(self, address: str, payload: bytes, **_kw: object) -> tuple[int, bytes]:
        self.requests.append(payload)
        view = json.loads(json.loads(payload)["messages"][1]["content"])
        if view["posting"]["company"] == "Example Robotics":
            content = (
                f"From the resume ({SENTINEL}) I see one gap: "
                '{"gaps":[{"name":"Forklift licence","kind":"minor"}],'
                f'"requirements":["Knows {SENTINEL}","Forklift licence course",'
                '"Weekend shifts"]}'
            )
        else:
            content = json.dumps({"gaps": [{"name": f"Has {SENTINEL}", "kind": "core"}]})
        body = {"choices": [{"message": {"content": content}}]}
        return 200, json.dumps(body).encode()


class FakeEmbedder:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def __call__(self, address: str, payload: bytes, **_kw: object) -> tuple[int, bytes]:
        text = json.loads(payload)["input"]
        self.inputs.append(text)
        vector = [1.0, 0.0] if SENTINEL in text else [0.6, 0.8]
        return 200, json.dumps({"data": [{"embedding": vector}]}).encode()


def _dump(path: Path) -> str:
    conn = db.connect_read_only(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_the_resume_reaches_the_model_and_nothing_else(
    inited: Path,
    probe: SystemProbe,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    source = tmp_path / f"synthetic.{kind}"
    if kind == "pdf":
        source.write_bytes(make_pdf([RESUME_LINE]))
    else:
        source.write_bytes(
            make_docx(document_xml(f"<w:p><w:r><w:t>{RESUME_LINE}</w:t></w:r></w:p>"))
        )

    # 1. Import. A file the importer refuses, with the sentinel inside it, first.
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n" + SENTINEL.encode() * 20)
    assert cli.main(["resume", "import", str(broken)], probe=probe) == cli.EXIT_ERROR
    assert cli.main(["resume", "import", str(source)], probe=probe) == cli.EXIT_OK
    stored = (inited / resume.RESUME_DIR / resume.CURRENT).read_text()
    assert SENTINEL in stored  # the one place the text is kept

    # 2. Watch, with fake boards, model and embeddings.
    model, embedder = FakeModel(), FakeEmbedder()
    monkeypatch.setattr(cli, "fetch_url", _boards)
    monkeypatch.setattr(cli, "score_transport", model)
    monkeypatch.setattr(embed, "default_transport", embedder)
    monkeypatch.setattr(cli, "config_today", lambda doc, now=None: dt.date(2026, 11, 1))
    assert cli.main(["watch"], probe=probe) == cli.EXIT_OK

    assert len(model.requests) == 2
    for request in model.requests:
        view = json.loads(json.loads(request)["messages"][1]["content"])
        assert SENTINEL in view["resume"]

    # 3. A bot poll sends the pending alerts.
    api = FakeTelegram()
    api.updates = [[]]
    datadir = DataDir(root=inited, mount_point=inited)
    assert (
        botmod.run(
            datadir,
            secret=lambda _n: TOKEN,
            api_factory=lambda _t: api,
            clock=Clock(),
            max_polls=1,
        )
        == 0
    )
    sent = [params for method, params in api.calls if method == "sendMessage"]
    assert len(sent) == 1  # the copied-gap reply was refused, so only one posting is scored
    # One digest. Of the key requirements, the one copying the resume and the one naming
    # the gap were dropped; the gap is a count, never its name.
    digest = sent[0]["text"]
    assert digest.startswith("New postings: 1\n\n1. Example Robotics")
    assert "| 1 gap" in digest and "Needs: Weekend shifts" in digest
    assert "Forklift" not in digest
    assert_absent("a Telegram message", json.dumps([params for _m, params in api.calls]))

    # Nothing else holds it.
    captured = capsys.readouterr()
    assert_absent("stdout", captured.out)
    assert_absent("stderr", captured.err)
    assert_absent("watch.log", (inited / "logs" / "watch.log").read_text())
    assert_absent("bot.log", (inited / "logs" / "bot.log").read_text())
    assert_absent("a log record", "\n".join(r.getMessage() for r in caplog.records))
    assert_absent("search.db", _dump(inited / "search.db"))
    for record in sorted(inited.glob("ei_record_*.db")):
        assert_absent(record.name, _dump(record))
    for leftover in (inited / "logs").iterdir():
        assert_absent(leftover.name, leftover.read_text(errors="replace"))

    # The refused reply leaves that posting unscored, to be tried again.
    conn = db.connect_read_only(inited / "search.db")
    try:
        rows = conn.execute(
            "SELECT c.name, p.llm_output, p.scored_resume_sha256 FROM postings p "
            "JOIN companies c ON c.id = p.company_id ORDER BY c.name"
        ).fetchall()
    finally:
        conn.close()
    by_company = {row[0]: (row[1], row[2]) for row in rows}
    assert by_company["Sample Logistics"] == (None, None)
    llm_output, sha = by_company["Example Robotics"]
    assert llm_output == '{"gaps":[{"kind":"minor","name":"Forklift licence"}]}'
    assert sha is not None and len(sha) == 64


def test_exceptions_on_the_import_path_carry_no_resume_text(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    for data in (
        b"%PDF-1.4\n" + SENTINEL.encode() * 50,
        make_docx(f"<w:document><w:t>{SENTINEL}</w:document>".encode()),
        make_docx(document_xml(f"<w:p><w:r><w:t>{SENTINEL * 2000}</w:t></w:r></w:p>")),
        SENTINEL.encode(),
    ):
        with pytest.raises(resume.ResumeError) as caught:
            resume.extract_text(data)
        errors.append(caught.value)
    path = tmp_path / f"{SENTINEL}.pdf"
    with pytest.raises(resume.ResumeError) as caught:
        resume.read_file(path)
    errors.append(caught.value)
    for exc in errors:
        assert_absent("exception text", str(exc))
        assert_absent("exception repr", repr(exc))
        assert_absent("a traceback", _traceback(exc))


def test_exceptions_on_the_scoring_path_carry_no_resume_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careerhunter.resume import Resume

    loaded = Resume(RESUME_LINE, "c" * 64)
    posting = PostingText("Example Robotics", "Operations Coordinator", "Example City")
    config = yaml.safe_load((Path(__file__).parents[1] / "config.example.yaml").read_text())

    def echoing(address: str, payload: bytes) -> tuple[int, bytes]:
        raise OSError(f"connection reset while sending {payload.decode()}")

    def status(address: str, payload: bytes) -> tuple[int, bytes]:
        return 500, payload  # a server that echoes the request back

    for transport in (echoing, status):
        with pytest.raises(ScoreError) as caught:
            score_posting(posting, config, transport=transport, resume=loaded)
        assert_absent("exception text", str(caught.value))
        assert_absent("exception repr", repr(caught.value))
        assert_absent("a traceback", _traceback(caught.value))

    assert_absent("Resume repr", repr(loaded))
    assert_absent("Resume str", str(loaded))

    # A resume changed after its import is refused without quoting it.
    conn = db.connect(tmp_path / "search.db")
    try:
        db.migrate(conn, "search")
        resume.import_text(tmp_path, conn, RESUME_LINE, now=lambda: "2026-11-04T12:00:00Z")
        (tmp_path / resume.RESUME_DIR / resume.CURRENT).write_text(SENTINEL)
        with pytest.raises(resume.ResumeError) as refused:
            resume.load(tmp_path, conn, {"search": {"resume": resume.CONFIG_VALUE}})
    finally:
        conn.close()
    assert_absent("exception text", str(refused.value))
    assert_absent("a traceback", _traceback(refused.value))
