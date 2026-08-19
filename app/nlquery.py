"""Natural-language query layer over the pipeline database.

Flow: question -> LLM writes one SELECT (constrained by the schema description)
-> SQL is validated in code -> executed on a READ-ONLY SQLite connection with a
row cap -> a second LLM call phrases the answer grounded ONLY in the returned
rows. The answer can't hallucinate data it never saw, and the generated SQL is
returned to the caller so every answer is auditable.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .db import DB_PATH
from .llm import CallBudget, call_agent
from .observability import log_event

MAX_ROWS = 200

SCHEMA_DESCRIPTION = """Tables in the SQLite database (one row in `runs` = one document pipeline run):

runs(run_id TEXT PK, doc_path TEXT, doc_name TEXT, customer_id TEXT,
     status TEXT/'running'|'completed'|'failed',
     decision TEXT/'auto_approve'|'human_review'|'amendment_request'|NULL,
     created_at TEXT 'YYYY-MM-DD HH:MM:SS', completed_at TEXT)

agent_steps(id, run_id, agent TEXT/'extractor'|'validator'|'router',
     status TEXT/'ok'|'error'|'skipped_resume', attempts INT, latency_ms INT,
     input_tokens INT, output_tokens INT, cost_usd REAL, error TEXT, created_at TEXT)

extracted_fields(run_id, field TEXT, value TEXT/NULL, confidence REAL, note TEXT)
  -- field is one of: consignee_name, hs_code, port_of_loading, port_of_discharge,
     incoterms, goods_description, gross_weight_kg, invoice_number

field_checks(run_id, field TEXT, status TEXT/'match'|'mismatch'|'uncertain',
     found TEXT, expected TEXT, reason TEXT)

Notes: dates are local time strings; "this week" means created_at >= date('now','-7 days').
"flagged" means decision IN ('human_review','amendment_request')."""

SQL_SYSTEM_PROMPT = f"""You translate a plain-English question about trade-document pipeline
runs into ONE SQLite SELECT statement.

{SCHEMA_DESCRIPTION}

Hard rules:
- Exactly one statement. SELECT (or WITH ... SELECT) only. No semicolons, no comments.
- Never modify data. Never use PRAGMA, ATTACH, or sqlite_master.
- Add LIMIT {MAX_ROWS} unless the query is an aggregate.
- If the question cannot be answered from this schema, set sql to null and explain why.
Respond ONLY via the structured output schema."""

ANSWER_SYSTEM_PROMPT = """You answer a user's question about trade-document pipeline runs.
You are given the question, the SQL that was executed, and the actual rows returned.
Answer ONLY from those rows — if the rows don't contain the answer, say so plainly.
Keep it to 1-3 sentences, concrete numbers first. Respond ONLY via the structured output schema."""

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|sqlite_master)\b|;|--|/\*",
    re.IGNORECASE,
)


def validate_sql(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise ValueError("only SELECT statements are allowed")
    if _FORBIDDEN.search(s):
        raise ValueError("query contains a forbidden keyword or statement separator")
    return s


def execute_readonly(sql: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchmany(MAX_ROWS)
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def answer_question(question: str, run_id: str = "nlquery") -> dict[str, Any]:
    budget = CallBudget(limit=4)

    sql_out, _ = await call_agent(
        run_id=run_id, agent="nl2sql",
        system_prompt=SQL_SYSTEM_PROMPT,
        prompt=f"Question: {question}",
        json_schema={
            "type": "object",
            "properties": {"sql": {"type": ["string", "null"]}, "explanation": {"type": "string"}},
            "required": ["sql", "explanation"],
            "additionalProperties": False,
        },
        budget=budget, max_turns=1, model="sonnet",
    )
    if not sql_out.get("sql"):
        return {"question": question, "sql": None, "rows": [],
                "answer": f"Can't answer from the pipeline database: {sql_out['explanation']}"}

    sql = validate_sql(sql_out["sql"])
    rows = execute_readonly(sql)
    log_event(run_id, "nlquery_executed", sql=sql, row_count=len(rows))

    ans_out, _ = await call_agent(
        run_id=run_id, agent="nl_answer",
        system_prompt=ANSWER_SYSTEM_PROMPT,
        prompt=(f"Question: {question}\nSQL executed: {sql}\n"
                f"Rows returned ({len(rows)}):\n{json.dumps(rows, default=str)[:8000]}"),
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        budget=budget, max_turns=1, model="sonnet",
    )
    return {"question": question, "sql": sql, "rows": rows, "answer": ans_out["answer"]}
