"""SQLite persistence: every run, agent step, extracted field, and check.

Three jobs:
  1. Crash-safe state: each agent's output is written the moment it completes,
     so a rerun with --resume rebuilds PipelineState from these rows and skips
     finished steps.
  2. Queryable record: extracted_fields and field_checks are first-class rows
     (not JSON blobs) so the NL->SQL layer can answer real questions.
  3. Observability: agent_steps carries latency + token usage per agent per run.

(Nova-scale note: this schema maps to ClickHouse tables in production; SQLite is
the right call for a single-tenant laptop demo.)
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .schemas import (
    ExtractionResult,
    PipelineState,
    RoutingDecision,
    StepMeta,
    ValidationResult,
)

DB_PATH = Path(__file__).resolve().parent.parent / "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    doc_path    TEXT NOT NULL,
    doc_name    TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | completed | failed
    decision    TEXT,                              -- auto_approve | human_review | amendment_request
    created_at  TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    agent       TEXT NOT NULL,                     -- extractor | validator | router
    status      TEXT NOT NULL,                     -- ok | error | skipped_resume
    attempts    INTEGER NOT NULL DEFAULT 1,
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL,
    output_json TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extracted_fields (
    run_id     TEXT NOT NULL REFERENCES runs(run_id),
    field      TEXT NOT NULL,
    value      TEXT,
    confidence REAL NOT NULL,
    note       TEXT,
    PRIMARY KEY (run_id, field)
);
CREATE TABLE IF NOT EXISTS field_checks (
    run_id   TEXT NOT NULL REFERENCES runs(run_id),
    field    TEXT NOT NULL,
    status   TEXT NOT NULL,                        -- match | mismatch | uncertain
    found    TEXT,
    expected TEXT,
    reason   TEXT,
    PRIMARY KEY (run_id, field)
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def create_run(run_id: str, doc_path: str, customer_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, doc_path, doc_name, customer_id, created_at) VALUES (?,?,?,?,?)",
            (run_id, doc_path, Path(doc_path).name, customer_id, _now()),
        )


def save_step(run_id: str, meta: StepMeta, output: Optional[dict] = None) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_steps (run_id, agent, status, attempts, latency_ms,
               input_tokens, output_tokens, cost_usd, output_json, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, meta.agent, meta.status, meta.attempts, meta.latency_ms,
             meta.input_tokens, meta.output_tokens, meta.cost_usd,
             json.dumps(output) if output is not None else None, meta.error, _now()),
        )


def save_extraction(run_id: str, extraction: ExtractionResult) -> None:
    with connect() as conn:
        for name, fv in extraction.fields.items():
            conn.execute(
                "INSERT OR REPLACE INTO extracted_fields (run_id, field, value, confidence, note) VALUES (?,?,?,?,?)",
                (run_id, name, fv.value, fv.confidence, fv.note),
            )


def save_checks(run_id: str, validation: ValidationResult) -> None:
    with connect() as conn:
        for c in validation.checks:
            conn.execute(
                "INSERT OR REPLACE INTO field_checks (run_id, field, status, found, expected, reason) VALUES (?,?,?,?,?,?)",
                (run_id, c.field, c.status.value, c.found, c.expected, c.reason),
            )


def finish_run(run_id: str, status: str, decision: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status=?, decision=?, completed_at=? WHERE run_id=?",
            (status, decision, _now(), run_id),
        )


def latest_step_outputs(run_id: str) -> dict[str, dict]:
    """Most recent successful output per agent, for crash resume."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT agent, output_json FROM agent_steps
               WHERE run_id=? AND status='ok' AND output_json IS NOT NULL
               ORDER BY id""",
            (run_id,),
        ).fetchall()
    return {r["agent"]: json.loads(r["output_json"]) for r in rows}


def hydrate_state(run_id: str) -> Optional[PipelineState]:
    """Rebuild PipelineState from persisted rows (returns None if run unknown)."""
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        return None
    outputs = latest_step_outputs(run_id)
    state = PipelineState(run_id=run_id, doc_path=run["doc_path"], customer_id=run["customer_id"])
    if "extractor" in outputs:
        state.extraction = ExtractionResult.model_validate(outputs["extractor"])
    if "validator" in outputs:
        state.validation = ValidationResult.model_validate(outputs["validator"])
    if "router" in outputs:
        state.decision = RoutingDecision.model_validate(outputs["router"])
    return state


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """Full run record for the UI: run row + steps + hydrated agent outputs."""
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return None
        steps = conn.execute(
            "SELECT agent, status, attempts, latency_ms, input_tokens, output_tokens, cost_usd, error, created_at "
            "FROM agent_steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    outputs = latest_step_outputs(run_id)
    return {
        "run": dict(run),
        "steps": [dict(s) for s in steps],
        "extraction": outputs.get("extractor"),
        "validation": outputs.get("validator"),
        "decision": outputs.get("router"),
    }


def list_runs(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT run_id, doc_name, customer_id, status, decision, created_at, completed_at "
            "FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
