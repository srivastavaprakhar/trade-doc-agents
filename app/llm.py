"""Single wrapper around the Claude Agent SDK used by every agent.

Design constraints (deliberate — see README "Auth & LLM client"):
  - Auth: CLAUDE_CODE_OAUTH_TOKEN (or an already-logged-in Claude Code CLI).
    No ANTHROPIC_API_KEY anywhere in this project.
  - Each agent step is one constrained, near-deterministic SDK query():
      * tools disabled except where explicitly needed (extractor gets Read only)
      * tight max_turns
      * native structured output (output_format=json_schema) + Pydantic
        validation on the way out — never free-text parsing
  - Cost/loop guards (the POC stand-in for LiteLLM-style budget control):
      * per-call timeout, capped retries per agent (MAX_ATTEMPTS)
      * per-run call budget (CallBudget) — the pipeline hard-stops rather than
        hanging or spinning if an agent misbehaves
  - Every call logs latency + token usage under the run_id.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from .observability import log_event
from .schemas import StepMeta

MAX_ATTEMPTS = 2          # 1 call + 1 retry per agent step, then fail loudly
CALL_TIMEOUT_S = 180      # vision extraction on a scan can be slow; reasoning steps finish far faster
RUN_CALL_BUDGET = 8       # max SDK calls per pipeline run (3 agents x 2 attempts + slack)


class BudgetExceeded(RuntimeError):
    pass


class AgentCallError(RuntimeError):
    def __init__(self, msg: str, meta: StepMeta):
        super().__init__(msg)
        self.meta = meta


@dataclass
class CallBudget:
    """Per-run guard: caps total LLM calls so no retry loop can run away."""
    limit: int = RUN_CALL_BUDGET
    used: int = 0
    total_cost_usd: float = 0.0

    def charge(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded(f"run call budget exhausted ({self.limit} LLM calls)")
        self.used += 1


def _usage_tokens(usage: Any) -> tuple[int, int]:
    """Total input (incl. prompt-cache reads/writes) and output tokens."""
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
    inp = sum(int(get(k, 0) or 0) for k in
              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return inp, int(get("output_tokens", 0) or 0)


async def _one_query(prompt: str, options: ClaudeAgentOptions) -> tuple[dict, ResultMessage]:
    """Run one SDK query, return (parsed JSON output, result message)."""
    last_text = ""
    result_msg: Optional[ResultMessage] = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    last_text = block.text
        elif isinstance(message, ResultMessage):
            result_msg = message
    if result_msg is None:
        raise RuntimeError("SDK stream ended without a ResultMessage")

    # Prefer the SDK's structured output; fall back to parsing the final text.
    structured = getattr(result_msg, "structured_output", None)
    if isinstance(structured, dict):
        return structured, result_msg
    raw = result_msg.result if isinstance(result_msg.result, str) and result_msg.result.strip() else last_text
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    return json.loads(raw), result_msg


async def call_agent(
    *,
    run_id: str,
    agent: str,
    prompt: str,
    system_prompt: str,
    json_schema: dict,
    budget: CallBudget,
    allowed_tools: Optional[list[str]] = None,
    max_turns: int = 1,
    model: str = "sonnet",
    timeout_s: int = CALL_TIMEOUT_S,
) -> tuple[dict, StepMeta]:
    """One governed agent step. Returns (schema-conformant dict, StepMeta)."""
    tools = allowed_tools or []
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        tools=tools,                      # empty list = no built-in tools at all
        allowed_tools=tools,              # pre-approve so nothing prompts
        max_turns=max_turns,
        setting_sources=[],               # ignore user/project Claude settings entirely
        output_format={"type": "json_schema", "schema": json_schema},
    )

    meta = StepMeta(agent=agent, status="error", attempts=0)
    start = time.monotonic()
    last_err: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        meta.attempts = attempt
        budget.charge()
        t0 = time.monotonic()
        try:
            data, result_msg = await asyncio.wait_for(_one_query(prompt, options), timeout=timeout_s)
            inp, out = _usage_tokens(result_msg.usage)
            meta.input_tokens += inp
            meta.output_tokens += out
            cost = getattr(result_msg, "total_cost_usd", None)
            if cost:
                meta.cost_usd = (meta.cost_usd or 0.0) + cost
                budget.total_cost_usd += cost
            meta.latency_ms = int((time.monotonic() - start) * 1000)
            meta.status = "ok"
            log_event(run_id, "agent_call_ok", agent=agent, attempt=attempt,
                      latency_ms=int((time.monotonic() - t0) * 1000),
                      input_tokens=inp, output_tokens=out, cost_usd=cost)
            return data, meta
        except (asyncio.TimeoutError, json.JSONDecodeError, RuntimeError, Exception) as e:  # noqa: BLE001
            if isinstance(e, BudgetExceeded):
                raise
            last_err = e
            log_event(run_id, "agent_call_error", agent=agent, attempt=attempt,
                      error=f"{type(e).__name__}: {e}")

    meta.latency_ms = int((time.monotonic() - start) * 1000)
    meta.error = f"{type(last_err).__name__}: {last_err}"
    raise AgentCallError(f"{agent} failed after {MAX_ATTEMPTS} attempts: {meta.error}", meta)
