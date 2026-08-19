# Trade Document Agents

A laptop-runnable multi-agent pipeline for trade documents: a vision **Extractor**
reads a Bill of Lading / Commercial Invoice (PDF or scan), a **Validator** checks
every field against a customer-specific rule set, and a **Router** decides
auto-approve / human review / amendment request — with evidence-grounded reasoning
stored for every decision. Everything is persisted in SQLite, queryable in plain
English, and shown in a single-screen UI.

Built as Part 1 of the GoComet Nova Day-At-Work assignment. See `PRD.md` for the
product thinking and `TECHNICAL_WRITEUP.md` for architecture, real failure modes,
and production-scale notes.

```
document ─▶ Extractor ─▶ Validator ─▶ Router ─▶ SQLite ─▶ UI + NL query
            (vision,     (rule set,   (decision +          ("how many were
             per-field    match/mis/   reasoning)            flagged this week?")
             confidence)  uncertain)
```

## Requirements

- **Python 3.10+** (built and tested on 3.12)
- **Claude Code CLI** installed and logged into a Claude subscription
  (`npm install -g @anthropic-ai/claude-code` — requires Node.js — or the native
  installer, which doesn't).
  The `claude-agent-sdk` pip package bundles a CLI binary; if the bundled binary
  isn't picked up on your platform, having `claude` on PATH is the fallback —
  check with `claude --version`.
- No `ANTHROPIC_API_KEY` needed anywhere. All LLM calls authenticate through a
  **Claude Code OAuth token** (your subscription).

## Setup (fresh laptop)

```bash
git clone https://github.com/srivastavaprakhar/trade-doc-agents.git && cd trade-doc-agents
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Auth: generate a long-lived OAuth token from your Claude subscription
claude setup-token          # prints a token; do this once
cp .env.example .env        # paste the token into .env as CLAUDE_CODE_OAUTH_TOKEN=...
export $(grep -v '^#' .env | xargs)   # or use direnv / your shell profile
```

> Only run the `export` **after** pasting the token — exporting an empty
> `CLAUDE_CODE_OAUTH_TOKEN` can shadow the CLI's stored login and cause a
> confusing auth failure.

> Treat the token like a password — it is your subscription. Never commit `.env`.
> On the machine where you ran `claude` login interactively, the SDK will also
> pick up the CLI's stored credentials automatically, so the env var is only
> strictly required on machines without an interactive login.

## Seed sample documents

```bash
.venv/bin/python scripts/generate_sample_docs.py
```

(The generator uses macOS system fonts for the fake-scan typography and falls
back to Pillow's default font elsewhere — it runs on any OS, but the messy scan
looks most realistic on macOS. The repo already ships the generated docs, so
this step is only needed if you want to regenerate them.)

Writes to `sample_docs/`:
- `commercial_invoice_clean.pdf` — well-formatted, satisfies every Customer X rule
- `bill_of_lading_clean.pdf` — matching B/L for the same shipment
- `commercial_invoice_messy.png` — simulated bad scan: skewed, noisy, **smudged HS
  code**, **missing Incoterms**, and **wrong discharge port** (Rotterdam vs Hamburg)

## Run the demo end-to-end

```bash
.venv/bin/uvicorn app.main:app --port 8000
# open http://localhost:8000
```

1. Pick `commercial_invoice_clean.pdf` → **Run pipeline** → all 8 fields match →
   **AUTO-APPROVE** (~30s, 3 LLM calls).
2. Pick `commercial_invoice_messy.png` → smudged HS code comes back `null` at low
   confidence (20–30% across my test runs — extraction is nondeterministic, but it
   never guesses a value), missing Incoterms is `uncertain`, Rotterdam is a
   `mismatch` → **DRAFT AMENDMENT REQUEST** with an actionable discrepancy list.
3. Ask the query box: *"How many shipments were flagged this week?"* — the answer
   comes from real SQL over real rows (expand "SQL + rows" to audit it).
   More copy-paste examples in `QUERIES.md`.

CLI alternative (no UI):

```bash
.venv/bin/python -c "
import asyncio; from app.graph import run_pipeline
s = asyncio.run(run_pipeline('sample_docs/commercial_invoice_messy.png'))
print(s.decision.decision.value); print(s.decision.reasoning)"
```

### Crash resume demo

State is persisted to SQLite after **every** agent step, and every graph node is
idempotent. Kill the process mid-pipeline, then:

```bash
curl -X POST localhost:8000/api/resume/<run_id>
# or, to simulate a crash yourself:
.venv/bin/python tests/test_graph.py crash
.venv/bin/python tests/test_graph.py resume
```

(The two test commands must be separate invocations, not `&&`-chained: the crash
simulation terminates with a non-zero exit code by design — that's the crash.)

The rerun skips completed steps (`skipped_resume` in the telemetry table) — the
already-paid extraction call is never repeated.

## Repo map

```
app/schemas.py        shared Pydantic contracts + JSON schemas (the agent handoff format)
app/llm.py            the single Agent SDK wrapper: structured output, timeout,
                      retry cap, per-run call budget, token/cost capture
app/agents/           extractor.py · validator.py · router.py (one file per agent)
app/graph.py          LangGraph orchestration + crash-resume
app/db.py             SQLite schema & persistence (runs, agent_steps, extracted_fields, field_checks)
app/nlquery.py        NL -> validated SELECT -> read-only execution -> grounded answer
app/observability.py  run_id-threaded JSONL logging (the POC's Langfuse/OTel stand-in)
app/main.py           FastAPI endpoints
web/index.html        single-screen UI (plain HTML/JS — no build step, per the brief's "if faster" option)
rules/customer_x.yaml Customer X validation rule set
scripts/              sample document generator
tests/                smoke + agent + graph/crash-resume test scripts used during the build
logs/pipeline.jsonl   created at runtime (gitignored) — grep any run_id to trace a document's full journey
```

## Design defaults chosen (and why)

- **Native structured output over prompt-only JSON.** The brief suggested driving
  JSON via system-prompt instruction; the Agent SDK has since shipped first-class
  `output_format={"type": "json_schema"}`, which is strictly more reliable. Used
  that, plus Pydantic validation on the way out. (The tool-loop never fought us —
  no agent needed a workaround.)
- **Vision via the SDK's `Read` tool.** The extractor is granted exactly one tool
  (`Read`) and reads the PDF/image itself — no manual base64 plumbing, and PDFs
  with multiple pages come along free. All other agents run with **zero tools,
  `max_turns=1`**.
- **Model: Sonnet for all agents** — extraction quality was indistinguishable from
  larger models on these docs, at a fraction of latency/cost. Swappable per-agent
  in one place (`app/llm.py`).
- **Plain HTML/JS UI** instead of a Vite React app: one screen, zero build step,
  nothing the demo needs from React. The brief explicitly allows this.
- **Deterministic policy guards over LLM discretion**: low-confidence fields are
  forced to `uncertain` in code; the router cannot emit `auto_approve` when any
  mismatch/uncertain exists. Models propose, code governs.
- **Cost/loop guards** (POC stand-in for LiteLLM-style controls): 2 attempts max
  per agent, 180s timeout per call, 8-LLM-calls hard budget per run.

## Scope note on auth

This project intentionally runs on a **personal Claude Code OAuth token** — my own
subscription, my own laptop, single user. That is fine for a personal build and
demo, but Anthropic's terms don't allow shipping a hosted product that runs other
end-users' claude.ai logins through it. A production multi-client deployment would
move to metered API keys / enterprise billing with LiteLLM-style routing and cost
controls — see `TECHNICAL_WRITEUP.md`.
