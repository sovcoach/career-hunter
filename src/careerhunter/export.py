"""EI job-search record export: CSV + PDF for one claim and a date range.

Service Canada can ask for a detailed record of the job search: who you applied to,
for what, when, how, and what happened next, plus other job-search activities. This
writes that record for a chosen date range into $CAREERHUNTER_DATA_DIR/exports/.

The export reads one file only: the claim's ei_record_<label>.db, opened read-only by
the caller. It never opens search.db, so nothing from the search layer can appear.

Which records fall in the range:
- exact dates: from <= date <= to
- week precision: the week (Monday..Sunday) overlaps the range
- unknown dates: always listed, in their own clearly labelled "date unknown" group,
  so nothing silently drops out of the record.

Voided applications and activities are left out. They stay in the database, with their
void reason, but they are not part of the job-search record.

Every application row carries a JD reference (ADR-024): `JD <first 8 of sha256>
(<source>)` for its current job description, else `JD missing`, or `JD missing
(pre-feature)` for one recorded before JDs were kept. Beside the CSVs and the PDF, the
JDs themselves go in a folder, `<stem>-job-descriptions/`: one `.txt` per application
that has one (a header with the employer, role, date, reference and hash, then the
text exactly as recorded), and a copy of the JD's original file. A claim file still at
EI migration 0001 has no JDs: every row is pre-feature and there is no folder.

Files are named with a timestamp and opened in exclusive-create mode, so an export
never overwrites an earlier one. They are created owner-only (0600) whatever the umask,
like every other file in the data directory, and the JD folder is 0700.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from careerhunter import ei_store
from careerhunter.claims import read_file_claim
from careerhunter.ei_store import EiJobDescription
from careerhunter.enums import METHOD_LABELS

APPLICATION_COLUMNS = (
    "employer",
    "role",
    "location",
    "work_model",
    "date_applied",
    "date_precision",
    "method",
    "current_status",
    "status_history",
    "posting_url",
    "job_description",
)
ACTIVITY_COLUMNS = ("date", "date_precision", "type", "description", "related_company")


@dataclass(frozen=True)
class ExportResult:
    claim: str
    applications_csv: Path
    activities_csv: Path
    pdf: Path
    application_count: int
    activity_count: int
    job_descriptions: Path | None = None  # the JD folder, when any JD was exported
    jd_count: int = 0


def _in_range(
    precision: str, day: str | None, week: str | None, start: dt.date, end: dt.date
) -> bool:
    if precision == "exact" and day:
        return start <= dt.date.fromisoformat(day) <= end
    if precision == "week" and week:
        monday = dt.date.fromisoformat(week)
        return monday <= end and monday + dt.timedelta(days=6) >= start
    return precision == "unknown"


def plural(count: int, singular: str, plural_form: str) -> str:
    return f"{count} {singular if count == 1 else plural_form}"


def display_date(precision: str, day: str | None, week: str | None) -> str:
    if precision == "exact" and day:
        return day
    if precision == "week" and week:
        return f"week of {week}"
    return "unknown"


def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
    # Dated rows first in chronological order, then the "date unknown" group.
    return (1 if row["date_precision"] == "unknown" else 0, row["_sort_date"] or "")


def collect_applications(
    conn: sqlite3.Connection, start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    apps = conn.execute(
        "SELECT id, correlation_id, employer, role, location, work_model, date_applied, "
        "date_precision, week_of, method, posting_url FROM applications WHERE voided = 0"
    ).fetchall()
    expected = ei_store.jd_expected(conn)
    current = ei_store.current_job_descriptions(conn)
    history: dict[int, list[sqlite3.Row]] = {}
    for h in conn.execute(
        "SELECT application_id, status, status_date FROM status_history ORDER BY id"
    ):
        history.setdefault(int(h["application_id"]), []).append(h)

    rows: list[dict[str, Any]] = []
    for a in apps:
        if not _in_range(a["date_precision"], a["date_applied"], a["week_of"], start, end):
            continue
        events = history.get(int(a["id"]), [])
        corr = str(a["correlation_id"])
        jd = current.get(corr)
        rows.append(
            {
                "employer": a["employer"],
                "role": a["role"],
                "location": a["location"],
                "work_model": a["work_model"] or "",
                "date_applied": display_date(a["date_precision"], a["date_applied"], a["week_of"]),
                "date_precision": a["date_precision"],
                "method": METHOD_LABELS.get(a["method"], a["method"]),
                "current_status": events[-1]["status"].replace("_", " ") if events else "",
                "status_history": "; ".join(
                    f"{e['status_date'] or 'date unknown'} {e['status'].replace('_', ' ')}"
                    for e in events
                ),
                "posting_url": a["posting_url"] or "",
                "job_description": ei_store.jd_reference(
                    jd.text_sha256 if jd else None,
                    jd.source if jd else None,
                    expected.get(corr, 0),
                ),
                "_sort_date": a["date_applied"] or a["week_of"],
                "_jd": jd,
            }
        )
    rows.sort(key=_sort_key)
    return rows


def collect_activities(
    conn: sqlite3.Connection, start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for a in conn.execute(
        "SELECT date, date_precision, week_of, type, description, related_company "
        "FROM activities WHERE voided = 0"
    ):
        if not _in_range(a["date_precision"], a["date"], a["week_of"], start, end):
            continue
        rows.append(
            {
                "date": display_date(a["date_precision"], a["date"], a["week_of"]),
                "date_precision": a["date_precision"],
                "type": a["type"].replace("_", " "),
                "description": a["description"],
                "related_company": a["related_company"] or "",
                "_sort_date": a["date"] or a["week_of"],
            }
        )
    rows.sort(key=_sort_key)
    return rows


def csv_safe(value: Any) -> str:
    """Neutralize spreadsheet formula injection for text opened in Excel or Numbers."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def create_private(path: Path) -> int:
    """Create `path` owner-only (0600), failing if it exists. Returns the open descriptor.

    The mode is given to the create call itself, so the file is never readable by others,
    even for a moment.
    """
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with os.fdopen(create_private(path), "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([csv_safe(row[c]) for c in columns])


def _write_pdf(
    path: Path,
    claim: str,
    start: dt.date,
    end: dt.date,
    generated: dt.datetime,
    apps: list[dict[str, Any]],
    acts: list[dict[str, Any]],
) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        LongTable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    cell = styles["BodyText"].clone("cell", fontSize=7.5, leading=9)
    head = styles["BodyText"].clone("head", fontSize=7.5, leading=9, fontName="Helvetica-Bold")

    def table(columns: list[tuple[str, str, float]], rows: list[dict[str, Any]]) -> LongTable:
        data = [[Paragraph(escape(label), head) for _, label, _ in columns]]
        data += [[Paragraph(escape(str(r[key])), cell) for key, _, _ in columns] for r in rows]
        t = LongTable(data, colWidths=[w * inch for _, _, w in columns], repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return t

    app_cols = [
        ("employer", "Employer", 1.3),
        ("role", "Role", 1.5),
        ("location", "Location", 1.0),
        ("date_applied", "Date applied", 0.95),
        ("method", "Method", 0.9),
        ("current_status", "Current status", 0.8),
        ("status_history", "Status history", 2.55),
        ("job_description", "Job description", 1.0),
    ]
    act_cols = [
        ("date", "Date", 1.1),
        ("type", "Type", 1.3),
        ("description", "Description", 5.0),
        ("related_company", "Related company", 2.6),
    ]

    story: list[Any] = [
        Paragraph("Job search record", styles["Title"]),
        Paragraph(
            escape(
                f"{claim}  ·  Period: {start.isoformat()} to {end.isoformat()}  ·  "
                f"Generated {generated.strftime('%Y-%m-%d %H:%M')}  ·  "
                f"{plural(len(apps), 'application', 'applications')}, "
                f"{plural(len(acts), 'other activity', 'other activities')}"
            ),
            styles["Normal"],
        ),
        Spacer(1, 0.2 * inch),
    ]

    def section(title: str, cols: list[tuple[str, str, float]], rows: list[dict[str, Any]]) -> None:
        story.append(Paragraph(escape(title), styles["Heading2"]))
        dated = [r for r in rows if r["date_precision"] != "unknown"]
        undated = [r for r in rows if r["date_precision"] == "unknown"]
        story.append(table(cols, dated) if dated else Paragraph("None in this period.", cell))
        if undated:
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph("Date unknown", styles["Heading4"]))
            story.append(table(cols, undated))
        story.append(Spacer(1, 0.25 * inch))

    section("Applications", app_cols, apps)
    section("Other job-search activities", act_cols, acts)

    with os.fdopen(create_private(path), "wb") as fh:
        SimpleDocTemplate(
            fh,
            pagesize=landscape(letter),
            leftMargin=0.5 * inch,
            rightMargin=0.5 * inch,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
            title="Job search record",
        ).build(story)


def _jd_text(row: dict[str, Any], jd: EiJobDescription) -> str:
    header = [
        f"Employer: {row['employer']}",
        f"Role: {row['role']}",
        f"Date applied: {row['date_applied']}",
        f"Reference: {row['job_description']}",
        f"SHA-256 of the text: {jd.text_sha256}",
        f"Original file: {jd.file_path or 'none'}",
        "",
        "",
    ]
    return "\n".join(header) + jd.text


def _copy_private(source: Path, target: Path) -> bool:
    """Copy a JD's original file owner-only. False when it can't be read."""
    try:
        data = source.read_bytes()
    except OSError:
        return False
    with os.fdopen(create_private(target), "wb") as fh:
        fh.write(data)
    return True


def _write_jds(folder: Path, data_dir: Path, apps: list[dict[str, Any]]) -> int:
    """One .txt per application with a JD, and a copy of each original file."""
    with_jd = [row for row in apps if row["_jd"] is not None]
    if not with_jd:
        return 0
    folder.mkdir(mode=0o700)  # new: the stem carries a timestamp, and nothing is replaced
    os.chmod(folder, 0o700)
    for index, row in enumerate(with_jd, start=1):
        jd: EiJobDescription = row["_jd"]
        name = f"{index:03d}_{jd.text_sha256[:8]}"
        with os.fdopen(create_private(folder / f"{name}.txt"), "w", encoding="utf-8") as fh:
            fh.write(_jd_text(row, jd))
        if jd.file_path and jd.file_type:
            _copy_private(data_dir / jd.file_path, folder / f"{name}_original.{jd.file_type}")
    return len(with_jd)


def export(
    conn: sqlite3.Connection,
    exports_dir: Path,
    start: dt.date,
    end: dt.date,
    now: dt.datetime | None = None,
    data_dir: Path | None = None,
) -> ExportResult:
    """Export the EI record on `conn` (one claim's file) for the range start..end.

    `data_dir` is where the JDs' original files are, by default the parent of
    `exports_dir`.
    """
    if start > end:
        raise ValueError(f"--from {start} is after --to {end}")
    file_claim = read_file_claim(conn)
    if file_claim is None:
        raise ValueError("this file is not a complete EI record (no claim row)")
    now = now or dt.datetime.now()
    apps = collect_applications(conn, start, end)
    acts = collect_activities(conn, start, end)

    period = f"from {file_claim.start}" + (f" to {file_claim.end}" if file_claim.end else "")
    heading = f"EI claim {file_claim.label} ({period})"
    stem = (
        f"ei_{file_claim.label}_{start.isoformat()}_{end.isoformat()}_"
        f"{now.strftime('%Y%m%dT%H%M%S')}"
    )
    folder = exports_dir / f"{stem}-job-descriptions"
    _write_csv(exports_dir / f"{stem}_applications.csv", APPLICATION_COLUMNS, apps)
    _write_csv(exports_dir / f"{stem}_activities.csv", ACTIVITY_COLUMNS, acts)
    _write_pdf(exports_dir / f"{stem}.pdf", heading, start, end, now, apps, acts)
    jd_count = _write_jds(folder, data_dir or exports_dir.parent, apps)
    return ExportResult(
        claim=file_claim.label,
        applications_csv=exports_dir / f"{stem}_applications.csv",
        activities_csv=exports_dir / f"{stem}_activities.csv",
        pdf=exports_dir / f"{stem}.pdf",
        application_count=len(apps),
        activity_count=len(acts),
        job_descriptions=folder if jd_count else None,
        jd_count=jd_count,
    )
