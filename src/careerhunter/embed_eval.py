"""Rank stored cosines against stored LLM tiers. This module does not call a model.

The report is a private file. It lists a posting id, a tier name, a cosine, and whether
the posting was applied to or skipped. It does not list company, title, resume text,
posting text, or vectors. A weighted blend is ranked only when embedding_weight is set,
and that blend is not written back to the database.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class EvalError(Exception):
    """A closed message. It names a config field, not a posting or a path."""


@dataclass(frozen=True)
class Row:
    """One stored cosine. The tier is None when it is missing or not in gap_tiers."""

    posting_id: int
    tier: str | None
    cosine: float
    applied: bool
    skipped: bool

    @property
    def outcome(self) -> str:
        if self.applied:
            return "applied"
        if self.skipped:
            return "skipped"
        return "undecided"


def tier_names(config: Mapping[str, Any]) -> list[str]:
    """gap_tiers names, best first. A missing list or a name with whitespace is an error."""
    rubric = config.get("fit_rubric")
    gap = rubric.get("gap_tiers") if isinstance(rubric, Mapping) else None
    tiers = gap.get("tiers") if isinstance(gap, Mapping) else None
    if not isinstance(tiers, list) or not tiers:
        raise EvalError("fit_rubric.gap_tiers is not set")
    names: list[str] = []
    for item in tiers:
        name = item.get("name") if isinstance(item, Mapping) else None
        if not isinstance(name, str) or not name.strip() or any(ch.isspace() for ch in name):
            raise EvalError("fit_rubric.gap_tiers names must be a single word")
        names.append(name.strip())
    if len(set(names)) != len(names):
        raise EvalError("fit_rubric.gap_tiers names must be unique")
    return names


def embedding_weight(config: Mapping[str, Any]) -> float | None:
    """The optional blend weight. None means the report does not rank a blend."""
    rubric = config.get("fit_rubric")
    if not isinstance(rubric, Mapping):
        return None
    value = rubric.get("embedding_weight")
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def tier_unit(tier: str | None, names: Sequence[str]) -> float | None:
    """1 for the best tier and 0 for the worst. Unknown tiers have no unit."""
    if tier is None or tier not in names:
        return None
    count = len(names)
    if count <= 1:
        return 1.0
    return (count - 1 - names.index(tier)) / (count - 1)


def midranks(values: Sequence[float]) -> list[float]:
    """1-based ranks. Tied values share the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end + 1) / 2
        for cursor in range(start, end + 1):
            ranks[order[cursor]] = rank
        start = end + 1
    return ranks


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Pearson correlation, or None when either side has no spread."""
    count = len(left)
    if count != len(right) or count < 2:
        return None
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    paired = [
        (item - mean_left, other - mean_right) for item, other in zip(left, right, strict=True)
    ]
    numerator = sum(one * other for one, other in paired)
    left_ss = sum(one * one for one, _other in paired)
    right_ss = sum(other * other for _one, other in paired)
    if left_ss == 0.0 or right_ss == 0.0:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Spearman correlation: Pearson on midranks. Undefined below two rows or with no spread."""
    if len(left) != len(right) or len(left) < 2:
        return None
    return pearson(midranks(left), midranks(right))


def precision_at(hits: Sequence[bool], k: int) -> float | None:
    """Hits in the first min(k, n) rows, divided by that count. Empty input is undefined."""
    if not hits or k < 1:
        return None
    take = min(k, len(hits))
    return sum(1 for hit in hits[:take] if hit) / take


def load_rows(conn: sqlite3.Connection, names: Sequence[str]) -> list[Row]:
    """Postings that already have a cosine. No company, title, or text column is read."""
    found: list[Row] = []
    query = (
        "SELECT p.id, p.embedding_score, p.scout_metadata, "
        "EXISTS(SELECT 1 FROM applications a WHERE a.posting_id = p.id) AS applied, "
        "EXISTS(SELECT 1 FROM skips s WHERE s.posting_id = p.id) AS skipped "
        "FROM postings p WHERE p.embedding_score IS NOT NULL ORDER BY p.id"
    )
    for record in conn.execute(query):
        cosine = float(record["embedding_score"])
        if not math.isfinite(cosine):
            continue
        found.append(
            Row(
                posting_id=int(record["id"]),
                tier=_tier_name(record["scout_metadata"], names),
                cosine=cosine,
                applied=bool(record["applied"]),
                skipped=bool(record["skipped"]),
            )
        )
    return found


def render(rows: Sequence[Row], names: Sequence[str], weight: float | None) -> str:
    """The report body. Metrics that cannot be computed are the word undefined."""
    units = [tier_unit(row.tier, names) for row in rows]
    paired = [(row.cosine, unit) for row, unit in zip(rows, units, strict=True) if unit is not None]
    cosine_rank = spearman([cosine for cosine, _unit in paired], [unit for _cosine, unit in paired])
    lines = [
        f"spearman: {_fmt(cosine_rank)}",
        f"precision_at_5: {_fmt(_precision(rows, None, names))}",
        f"precision_at_10: {_fmt(_precision(rows, None, names, k=10))}",
    ]
    if weight is not None:
        blends = [(_blend(cosine, unit, weight), unit) for cosine, unit in paired]
        blend_rank = spearman([score for score, _unit in blends], [unit for _score, unit in blends])
        lines.extend(
            [
                f"embedding_weight: {weight:.6f}",
                f"blend_spearman: {_fmt(blend_rank)}",
                f"blend_precision_at_5: {_fmt(_precision(rows, weight, names))}",
                f"blend_precision_at_10: {_fmt(_precision(rows, weight, names, k=10))}",
            ]
        )
    header = "posting_id tier cosine outcome"
    if weight is not None:
        header += " blend"
    lines.append(header)
    for row, unit in zip(rows, units, strict=True):
        tier = row.tier if row.tier is not None else "-"
        cells = [str(row.posting_id), tier, f"{row.cosine:.6f}", row.outcome]
        if weight is not None:
            shown = "-" if unit is None else f"{_blend(row.cosine, unit, weight):.6f}"
            cells.append(shown)
        lines.append(" ".join(cells))
    return "\n".join(lines) + "\n"


def filename(now: dt.datetime) -> str:
    """embed-eval-YYYYMMDDTHHMMSSZ.txt. The stamp is UTC and has no colon."""
    stamp = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"embed-eval-{stamp}.txt"


def write_report(root: Path, text: str, now: dt.datetime) -> Path:
    """Write evals/<filename> mode 0600. Refuses to replace an existing file."""
    folder = root / "evals"
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / filename(now)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path


def _blend(cosine: float, unit: float, weight: float) -> float:
    """weight * cosine + (1 - weight) * tier unit. The caller does not store this."""
    return weight * cosine + (1.0 - weight) * unit


def _fmt(value: float | None) -> str:
    if value is None:
        return "undefined"
    return f"{value:.6f}"


def _precision(
    rows: Sequence[Row],
    weight: float | None,
    names: Sequence[str],
    *,
    k: int = 5,
) -> float | None:
    """Decided rows only, highest score first. A missing tier drops out of a blend."""
    ranked: list[tuple[float, int, bool]] = []
    for row in rows:
        if not row.applied and not row.skipped:
            continue
        if weight is None:
            score = row.cosine
        else:
            unit = tier_unit(row.tier, names)
            if unit is None:
                continue
            score = _blend(row.cosine, unit, weight)
        ranked.append((score, row.posting_id, row.applied))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return precision_at([applied for _score, _posting_id, applied in ranked], k)


def _tier_name(raw: object, names: Sequence[str]) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    tier = payload.get("tier")
    if not isinstance(tier, str):
        return None
    cleaned = tier.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned) or cleaned not in names:
        return None
    return cleaned
