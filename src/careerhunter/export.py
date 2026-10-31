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

Files are named with a timestamp and opened in exclusive-create mode, so an export
never overwrites an earlier one.
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from careerhunter.claims import read_file_claim
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
        "SELECT id, employer, role, location, work_model, date_applied, date_precision, "
        "week_of, method, posting_url FROM applications WHERE voided = 0"
    ).fetchall()
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
                "_sort_date": a["date_applied"] or a["week_of"],
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


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("x", newline="", encoding="utf-8") as fh:
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
        ("status_history", "Status history", 3.55),
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

    with path.open("xb") as fh:
        SimpleDocTemplate(
            fh,
            pagesize=landscape(letter),
            leftMargin=0.5 * inch,
            rightMargin=0.5 * inch,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
            title="Job search record",
        ).build(story)


def export(
    conn: sqlite3.Connection,
    exports_dir: Path,
    start: dt.date,
    end: dt.date,
    now: dt.datetime | None = None,
) -> ExportResult:
    """Export the EI record on `conn` (one claim's file) for the range start..end."""
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
    result = ExportResult(
        claim=file_claim.label,
        applications_csv=exports_dir / f"{stem}_applications.csv",
        activities_csv=exports_dir / f"{stem}_activities.csv",
        pdf=exports_dir / f"{stem}.pdf",
        application_count=len(apps),
        activity_count=len(acts),
    )
    _write_csv(result.applications_csv, APPLICATION_COLUMNS, apps)
    _write_csv(result.activities_csv, ACTIVITY_COLUMNS, acts)
    _write_pdf(result.pdf, heading, start, end, now, apps, acts)
    return result
