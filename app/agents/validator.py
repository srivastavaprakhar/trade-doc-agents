"""Agent 2 — Validator.

Checks the extraction against one customer's rule set (rules/<customer>.yaml)
and returns a field-by-field verdict: match / mismatch / uncertain.

Governance layer: the LLM proposes verdicts (it is good at fuzzy judgments like
"MERIDIAN TRADING GmBH" ~ "Meridian Trading GmbH"), but deterministic code has
the last word on trust: any field the extractor read with confidence below
CONFIDENCE_FLOOR, or returned as null, is FORCED to `uncertain`/flagged — the
model is never allowed to silently pass a low-confidence field.
"""

import json
from pathlib import Path

import yaml

from ..llm import CallBudget, call_agent
from ..schemas import (
    CONFIDENCE_FLOOR,
    CheckStatus,
    ExtractionResult,
    FieldCheck,
    StepMeta,
    ValidationResult,
    validation_json_schema,
)

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "rules"

SYSTEM_PROMPT = """You are a trade-document validation agent. You receive (a) fields
extracted from one document, each with a confidence score, and (b) a customer rule set.

For EVERY field in the rule set's required_fields, emit exactly one check with:
- status: "match" (satisfies the rule), "mismatch" (clearly violates it), or
  "uncertain" (can't tell: value missing, ambiguous, or only partially readable)
- found: the extracted value (null if missing)
- expected: a short human-readable statement of the rule (e.g. "one of CIF, FOB")
- reason: one concrete sentence grounded in the found value and the rule.

Judgment rules:
1. Fuzzy-match fields marked match: fuzzy in the rules: trivial casing/spacing/typo
   differences are a match, but say so in the reason.
2. A null/missing value for a required field is NEVER a match.
3. Do not invent values that are not in the extraction input.
4. Respond ONLY via the structured output schema."""


def load_rules(customer_id: str) -> dict:
    with open(RULES_DIR / f"{customer_id}.yaml") as f:
        return yaml.safe_load(f)


def _enforce_trust_floor(result: ValidationResult, extraction: ExtractionResult, rules: dict) -> ValidationResult:
    """Deterministic guardrails over the LLM's verdicts."""
    required = rules.get("required_fields", [])
    by_field = {c.field: c for c in result.checks}

    for name in required:
        fv = extraction.fields.get(name)
        check = by_field.get(name)
        if check is None:
            # LLM skipped a required field -> add an explicit uncertain check.
            check = FieldCheck(field=name, status=CheckStatus.UNCERTAIN,
                               found=fv.value if fv else None,
                               expected="required by customer rule set",
                               reason="Validator did not evaluate this required field; flagged by policy guard.")
            result.checks.append(check)
            by_field[name] = check
            continue
        if fv is None or fv.value is None:
            if check.status == CheckStatus.MATCH:
                check.status = CheckStatus.UNCERTAIN
                check.reason += " [policy guard: value missing/unreadable — cannot be a match]"
        elif fv.confidence < CONFIDENCE_FLOOR and check.status == CheckStatus.MATCH:
            check.status = CheckStatus.UNCERTAIN
            check.reason += (f" [policy guard: extractor confidence {fv.confidence:.2f} "
                             f"is below the {CONFIDENCE_FLOOR} trust floor]")
    return result


async def run_validator(run_id: str, extraction: ExtractionResult, customer_id: str,
                        budget: CallBudget) -> tuple[ValidationResult, StepMeta]:
    rules = load_rules(customer_id)
    prompt = (
        "Customer rule set (YAML):\n" + yaml.safe_dump(rules, sort_keys=False)
        + "\nExtracted document fields (JSON, with per-field confidence):\n"
        + json.dumps(extraction.model_dump(), indent=2)
        + "\nValidate every required field and respond via the structured output schema only."
    )
    data, meta = await call_agent(
        run_id=run_id,
        agent="validator",
        system_prompt=SYSTEM_PROMPT,
        prompt=prompt,
        json_schema=validation_json_schema(),
        budget=budget,
        max_turns=1,
        model="sonnet",
    )
    result = ValidationResult(customer_id=customer_id, **data)
    return _enforce_trust_floor(result, extraction, rules), meta
