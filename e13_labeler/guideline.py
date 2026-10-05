"""
The labelling guideline (requirements FR-26): versioned, and the gate before
the quiz.

The text lives in ``guideline.json`` (or the file named by ``E13_GUIDELINE``)
so the owner can revise it without touching code. Definitions and span rules
come from ``reasons.py``, so the page can't drift from what the app enforces.
Every annotation records the version in force.
"""

import json
import os
import sqlite3
from functools import lru_cache
from pathlib import Path

from .reasons import CANDIDATES, DEFAULT_SPAN_POLICY, DEFINITIONS, HARD_SPAN_RULES, REASONS

DEFAULT_PATH = Path(__file__).with_name("guideline.json")
# Milliseconds, so "viewed after the failed attempt" holds within one second
NOW_MS = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

SPAN_RULES = {
    "conflicting_evidence": "Required, always: a support span and a refute span on the same option.",
    "non_factual_support": "Required: a framing span (the hedge or attribution) and the support span it frames.",
    "stale_state": "Required, always: the dated or time-sensitive phrase.",
    "false_premise": "Required: the text that contradicts the presupposition (role refute).",
    "unrelated": "No span.",
}


def _path() -> Path:
    return Path(os.environ.get("E13_GUIDELINE", DEFAULT_PATH))


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [r for r in REASONS if r not in data.get("reasons", {})]
    if missing or not data.get("version"):
        raise ValueError(f"guideline {path}: needs a version and every reason (missing {missing})")
    return data


def load() -> dict:
    path = _path()
    return _load(str(path), path.stat().st_mtime)


def version() -> str:
    return str(load()["version"])


def span_rule(reason: str) -> str:
    if reason in SPAN_RULES:
        return SPAN_RULES[reason]
    policy = DEFAULT_SPAN_POLICY.get(reason, "optional")
    return {"required": "Required.", "optional": "Optional, but mark it when one phrase shows it.",
            "none": "No span."}[policy]


def page() -> dict:
    """The guideline page: per reason, definition, positive example, near-miss negative, span rule."""
    data = load()
    return {
        "version": str(data["version"]),
        "status": data.get("status"),
        "intro": data.get("intro", ""),
        "spans": data.get("spans", ""),
        "reasons": [{
            "key": r, "candidate": r in CANDIDATES, "definition": DEFINITIONS[r],
            "span_rule": span_rule(r), "span_rule_hard": r in HARD_SPAN_RULES,
            "positive": data["reasons"][r]["positive"], "negative": data["reasons"][r]["negative"],
        } for r in REASONS],
    }


def record_view(conn: sqlite3.Connection, labeler_id: int) -> None:
    conn.execute("INSERT INTO guideline_views (labeler_id, version, viewed_at) VALUES (?, ?, %s)" % NOW_MS,
                 (labeler_id, version()))


def viewed_since(conn: sqlite3.Connection, labeler_id: int, since: str = None) -> bool:
    """Has this labeler opened the current version (after ``since``, an SQLite timestamp)?"""
    sql = "SELECT 1 FROM guideline_views WHERE labeler_id = ? AND version = ?"
    params = [labeler_id, version()]
    if since:
        sql += " AND viewed_at > ?"
        params.append(since)
    return conn.execute(sql, params).fetchone() is not None
