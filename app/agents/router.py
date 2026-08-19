"""Agent 3 — Router / Decision.

Turns the validation result into one of three outcomes with evidence-grounded
reasoning (the POC's version of Nova's "evidence delivery" stage — the reasoning
must cite the specific fields that drove the decision, and it is stored + shown
in the UI).

Governance layer: the LLM writes the reasoning and picks a decision, but a
deterministic policy computes which decisions are even allowed:
  - any mismatch            -> {amendment_request, human_review}   (never auto-approve)
  - else any uncertain      -> {human_review}
  - all match               -> {auto_approve, human_review}
If the LLM picks outside its allowed set, code overrides to the safe default and
appends a note — the model can never approve a shipment with a known discrepancy.
"""

import json

from ..llm import CallBudget, call_agent
from ..schemas import Decision, RoutingDecision, StepMeta, ValidationResult, routing_json_schema

SYSTEM_PROMPT = """You are the routing agent in a trade-document pipeline. You receive a
field-by-field validation result for one shipment document and must decide:

- "auto_approve": every check is a match; no human needed.
- "human_review": something is uncertain (unreadable field, low confidence, ambiguous
  rule match) — a human must look, but no discrepancy is proven yet.
- "amendment_request": at least one clear mismatch against the customer's rules — draft
  the list of discrepancies the supplier must amend.

Requirements:
1. reasoning must cite the specific fields and found-vs-expected values that drove the
   decision — concrete evidence, not generalities. 2-5 sentences.
2. discrepancies: one entry per mismatched or uncertain field, phrased as an actionable
   line for an amendment email (e.g. "Port of discharge shows 'Rotterdam (NLRTM)' but
   the order requires Hamburg (DEHAM) — please reissue the invoice."). Empty if all match.
3. confidence reflects how clear-cut the decision is.
4. Respond ONLY via the structured output schema."""


def _allowed(validation: ValidationResult) -> tuple[set[Decision], Decision]:
    counts = validation.counts()
    if counts["mismatch"] > 0:
        return {Decision.AMENDMENT_REQUEST, Decision.HUMAN_REVIEW}, Decision.AMENDMENT_REQUEST
    if counts["uncertain"] > 0:
        return {Decision.HUMAN_REVIEW}, Decision.HUMAN_REVIEW
    return {Decision.AUTO_APPROVE, Decision.HUMAN_REVIEW}, Decision.AUTO_APPROVE


async def run_router(run_id: str, validation: ValidationResult,
                     budget: CallBudget) -> tuple[RoutingDecision, StepMeta]:
    prompt = (
        "Validation result for one shipment document (JSON):\n"
        + json.dumps(validation.model_dump(), indent=2)
        + f"\nStatus counts: {validation.counts()}\n"
        "Decide the routing outcome and respond via the structured output schema only."
    )
    data, meta = await call_agent(
        run_id=run_id,
        agent="router",
        system_prompt=SYSTEM_PROMPT,
        prompt=prompt,
        json_schema=routing_json_schema(),
        budget=budget,
        max_turns=1,
        model="sonnet",
    )
    decision = RoutingDecision.model_validate(data)

    allowed, default = _allowed(validation)
    if decision.decision not in allowed:
        decision.reasoning += (
            f" [policy guard: model chose '{decision.decision.value}', which is not permitted "
            f"given validation counts {validation.counts()}; overridden to '{default.value}'.]"
        )
        decision.decision = default
    return decision, meta
