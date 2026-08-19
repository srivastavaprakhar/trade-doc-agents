"""LangGraph orchestration: extract -> validate -> route.

Each agent is one node; the shared PipelineState (Pydantic) is the only thing
that flows between them — no raw-text chaining. Crash-safety comes from two
properties, not from a framework feature:

  1. Every node persists its output to SQLite the moment it finishes.
  2. Every node is idempotent: if its output already exists in state (because
     the state was hydrated from SQLite on a resume), it records
     'skipped_resume' and passes through untouched.

So `run_pipeline(..., resume=True)` after a crash re-executes only the steps
that never completed. (Nova mapping: this graph is the "plan + execute" stage;
the customer rule-set selection in the validator is "schema routing"; the
router's stored reasoning is "evidence delivery".)

Loop/cost guards: retries are capped in llm.py, each call has a timeout, and a
per-run CallBudget hard-stops the whole graph — the POC's stand-in for
LiteLLM-style budget control.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from langgraph.graph import END, StateGraph

from . import db
from .agents.extractor import run_extractor
from .agents.router import run_router
from .agents.validator import run_validator
from .llm import AgentCallError, BudgetExceeded, CallBudget
from .observability import log_event
from .schemas import PipelineState, StepMeta

# One CallBudget per run_id, shared across the run's nodes.
_budgets: dict[str, CallBudget] = {}


def _budget(run_id: str) -> CallBudget:
    return _budgets.setdefault(run_id, CallBudget())


def _skip(state: PipelineState, agent: str) -> dict:
    log_event(state.run_id, "node_skipped_resume", agent=agent)
    meta = StepMeta(agent=agent, status="skipped_resume", attempts=0)
    db.save_step(state.run_id, meta)
    return {"step_meta": state.step_meta + [meta]}


def _fail(state: PipelineState, agent: str, err: Exception) -> dict:
    log_event(state.run_id, "node_failed", agent=agent, error=str(err))
    meta = err.meta if isinstance(err, AgentCallError) else StepMeta(agent=agent, status="error", error=str(err))
    db.save_step(state.run_id, meta)
    return {"failed": True, "errors": state.errors + [f"{agent}: {err}"],
            "step_meta": state.step_meta + [meta]}


async def extract_node(state: PipelineState) -> dict:
    if state.extraction is not None:
        return _skip(state, "extractor")
    try:
        extraction, meta = await run_extractor(state.run_id, state.doc_path, _budget(state.run_id))
    except (AgentCallError, BudgetExceeded, FileNotFoundError) as e:
        return _fail(state, "extractor", e)
    db.save_step(state.run_id, meta, extraction.model_dump())
    db.save_extraction(state.run_id, extraction)
    return {"extraction": extraction, "step_meta": state.step_meta + [meta]}


async def validate_node(state: PipelineState) -> dict:
    if state.validation is not None:
        return _skip(state, "validator")
    try:
        validation, meta = await run_validator(state.run_id, state.extraction, state.customer_id,
                                               _budget(state.run_id))
    except (AgentCallError, BudgetExceeded) as e:
        return _fail(state, "validator", e)
    db.save_step(state.run_id, meta, validation.model_dump())
    db.save_checks(state.run_id, validation)
    return {"validation": validation, "step_meta": state.step_meta + [meta]}


async def route_node(state: PipelineState) -> dict:
    if state.decision is not None:
        return _skip(state, "router")
    try:
        decision, meta = await run_router(state.run_id, state.validation, _budget(state.run_id))
    except (AgentCallError, BudgetExceeded) as e:
        return _fail(state, "router", e)
    db.save_step(state.run_id, meta, decision.model_dump())
    return {"decision": decision, "step_meta": state.step_meta + [meta]}


def _continue_unless_failed(state: PipelineState) -> str:
    return "fail" if state.failed else "ok"


def build_graph():
    g = StateGraph(PipelineState)
    g.add_node("extract", extract_node)
    g.add_node("validate", validate_node)
    g.add_node("route", route_node)
    g.set_entry_point("extract")
    g.add_conditional_edges("extract", _continue_unless_failed, {"ok": "validate", "fail": END})
    g.add_conditional_edges("validate", _continue_unless_failed, {"ok": "route", "fail": END})
    g.add_edge("route", END)
    return g.compile()


GRAPH = build_graph()


async def run_pipeline(doc_path: str, customer_id: str = "customer_x",
                       run_id: str | None = None, resume: bool = False) -> PipelineState:
    """Execute (or resume) one document's pipeline; returns the final state."""
    db.init_db()
    if resume and run_id:
        state = db.hydrate_state(run_id)
        if state is None:
            raise ValueError(f"unknown run_id {run_id!r} — nothing to resume")
        log_event(run_id, "pipeline_resume", have=[a for a, v in
                  [("extractor", state.extraction), ("validator", state.validation),
                   ("router", state.decision)] if v is not None])
    else:
        run_id = run_id or uuid.uuid4().hex[:12]
        state = PipelineState(run_id=run_id, doc_path=str(Path(doc_path).resolve()),
                              customer_id=customer_id)
        db.create_run(run_id, state.doc_path, customer_id)
        log_event(run_id, "pipeline_start", doc=state.doc_path, customer=customer_id)

    final = PipelineState.model_validate(await GRAPH.ainvoke(state))
    _budgets.pop(final.run_id, None)

    if final.failed:
        db.finish_run(final.run_id, "failed")
        log_event(final.run_id, "pipeline_failed", errors=final.errors)
    else:
        db.finish_run(final.run_id, "completed", final.decision.decision.value)
        log_event(final.run_id, "pipeline_completed", decision=final.decision.decision.value,
                  total_tokens=sum(m.input_tokens + m.output_tokens for m in final.step_meta),
                  total_cost_usd=round(sum(m.cost_usd or 0 for m in final.step_meta), 4))
    return final
