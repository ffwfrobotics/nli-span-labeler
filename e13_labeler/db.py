"""
SQLite storage: connections and the E13 schema.

The schema is requirements §5.1. It replaces the premise/hypothesis tables of
the old app (``examples``, ``labels``, ``span_selections``, ``complexity_scores``
and so on), which had no data worth migrating (requirements §3.2).

Migrations are an ordered list of SQL scripts tracked with ``PRAGMA
user_version``. Add new ones at the end; never edit one that has shipped.
"""

import json
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA_V1 = """
CREATE TABLE labelers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pseudonym TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('human', 'model')),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'labeler', 'model')),
    clearance TEXT NOT NULL DEFAULT 'public' CHECK (clearance IN ('public', 'internal')),
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'onboarding', 'active', 'paused', 'revoked')),
    login_name TEXT UNIQUE,
    password_hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT
);

-- Owner-only; optional; deletable (NFR-4). Never exported.
CREATE TABLE identity (
    labeler_id INTEGER PRIMARY KEY REFERENCES labelers(id),
    contact TEXT,
    notes TEXT
);

CREATE TABLE invites (
    token_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    clearance TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_by INTEGER REFERENCES labelers(id),
    used_by INTEGER REFERENCES labelers(id)
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    labeler_id INTEGER NOT NULL REFERENCES labelers(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);
CREATE INDEX idx_sessions_labeler ON sessions(labeler_id);

CREATE TABLE import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    n_rows INTEGER NOT NULL DEFAULT 0,
    n_items INTEGER NOT NULL DEFAULT 0,
    n_rejected INTEGER NOT NULL DEFAULT 0,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE items (
    item_id TEXT PRIMARY KEY,                  -- "<row_id>#<qid>"
    row_id TEXT NOT NULL,
    qid TEXT NOT NULL,
    source TEXT NOT NULL,
    split TEXT,
    heldout INTEGER,
    state TEXT NOT NULL,                       -- exactly as imported (FR-5)
    state_format TEXT NOT NULL CHECK (state_format IN ('text', 'json')),
    state_sha256 TEXT NOT NULL,
    question_json TEXT NOT NULL,
    gold_json TEXT,
    permissions TEXT NOT NULL
        CHECK (permissions IN ('libre', 'restricted', 'jev', 'jev+restricted')),
    source_license TEXT,
    e13_json TEXT,
    model_answers_json TEXT,
    import_run_id INTEGER REFERENCES import_runs(id),
    UNIQUE (row_id, qid)
);
CREATE INDEX idx_items_row ON items(row_id);
CREATE INDEX idx_items_permissions ON items(permissions);

CREATE TABLE batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'reasons'
        CHECK (task_type IN ('reasons', 'reasons+relation', 'relation')),
    reason_set_json TEXT NOT NULL,
    overlap_target INTEGER NOT NULL DEFAULT 2 CHECK (overlap_target >= 2),
    tier_ceiling TEXT NOT NULL DEFAULT 'jev+restricted',
    span_policy_json TEXT NOT NULL DEFAULT '{}',
    show_model_answer TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'closed')),
    guideline_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE batch_items (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    item_id TEXT NOT NULL REFERENCES items(item_id),
    PRIMARY KEY (batch_id, item_id)
);
CREATE INDEX idx_batch_items_item ON batch_items(item_id);

CREATE TABLE locks (
    item_id TEXT PRIMARY KEY REFERENCES items(item_id),
    labeler_id INTEGER NOT NULL REFERENCES labelers(id),
    until TEXT NOT NULL
);

CREATE TABLE annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    batch_id INTEGER REFERENCES batches(id),
    labeler_id INTEGER NOT NULL REFERENCES labelers(id),
    version INTEGER NOT NULL DEFAULT 1,
    answerable INTEGER,
    reasons_json TEXT,          -- {"unrelated": true|false|null, ...}; null = not asked
    note TEXT,
    relation_json TEXT,
    skipped_code TEXT CHECK (skipped_code IS NULL OR skipped_code IN
        ('cannot_judge', 'broken_item', 'offensive', 'too_long', 'other')),
    policy_override INTEGER NOT NULL DEFAULT 0,
    is_gold_probe INTEGER NOT NULL DEFAULT 0,
    active_ms INTEGER,
    wall_ms INTEGER,
    position_in_state_run INTEGER,
    guideline_version TEXT,
    app_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (item_id, labeler_id, version)
);
CREATE INDEX idx_annotations_labeler ON annotations(labeler_id);
CREATE INDEX idx_annotations_batch ON annotations(batch_id);

CREATE TABLE spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id INTEGER NOT NULL REFERENCES annotations(id),
    side TEXT NOT NULL CHECK (side IN ('state', 'option')),
    option TEXT,
    pointer TEXT,
    start INTEGER,
    "end" INTEGER,
    text TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('support', 'refute', 'unsupported', 'framing')),
    reasons_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_spans_annotation ON spans(annotation_id);

CREATE TABLE gold (
    item_id TEXT PRIMARY KEY REFERENCES items(item_id),
    reasons_json TEXT NOT NULL,
    spans_json TEXT NOT NULL DEFAULT '[]',
    alternatives_json TEXT NOT NULL DEFAULT '{}',
    explanation TEXT,
    created_by INTEGER REFERENCES labelers(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retired INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    labeler_id INTEGER NOT NULL REFERENCES labelers(id),
    guideline_version TEXT,
    items_json TEXT NOT NULL,
    score REAL,
    passed INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE adjudications (
    item_id TEXT PRIMARY KEY REFERENCES items(item_id),
    batch_id INTEGER REFERENCES batches(id),
    reasons_json TEXT NOT NULL,
    spans_json TEXT NOT NULL DEFAULT '[]',
    adjudicator_id INTEGER REFERENCES labelers(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    labeler_id INTEGER NOT NULL REFERENCES labelers(id),
    kind TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES labelers(id),
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# v2: batch option to require a note for some reasons (FR-14); when a lock was
# handed out, for server-side wall time (FR-22).
SCHEMA_V2 = """
ALTER TABLE batches ADD COLUMN require_note INTEGER NOT NULL DEFAULT 0;
ALTER TABLE locks ADD COLUMN served_at TEXT;
"""

# v3: the queue's per-item label counts and "labelled by me" checks (FR-32, NFR-1)
SCHEMA_V3 = """
CREATE INDEX idx_annotations_item_batch ON annotations(item_id, batch_id);
CREATE INDEX idx_annotations_item_labeler ON annotations(item_id, labeler_id);
CREATE INDEX idx_locks_labeler ON locks(labeler_id);
"""

# v4: who may see an item, separate from its release tier (owner decision
# 2026-10-05): visibility comes from the text's licence only; Jev output only
# affects the release tier.
SCHEMA_V4 = """
ALTER TABLE items ADD COLUMN visibility TEXT NOT NULL DEFAULT 'restricted'
    CHECK (visibility IN ('libre', 'restricted'));
UPDATE items SET visibility = CASE WHEN permissions IN ('libre', 'jev') THEN 'libre' ELSE 'restricted' END;
CREATE INDEX idx_items_visibility ON items(visibility);
"""

MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4]


def connect() -> sqlite3.Connection:
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db():
    """Database connection context manager: commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> int:
    """Apply pending migrations. Returns the schema version."""
    with get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        return len(MIGRATIONS)


def audit(conn: sqlite3.Connection, actor_id, action: str, target: str = None, detail: dict = None):
    """Append an audit_log row (FR-53)."""
    conn.execute(
        "INSERT INTO audit_log (actor_id, action, target, detail_json) VALUES (?, ?, ?, ?)",
        (actor_id, action, target, json.dumps(detail) if detail is not None else None),
    )
