"""Shared Pydantic schemas — the contract every agent handoff flows through.

Nothing between agents is raw text: the extractor emits ExtractionResult, the
validator consumes it and emits ValidationResult, the router consumes that and
emits RoutingDecision. PipelineState is the LangGraph state object and is
persisted to SQLite after every node so a crashed run can resume.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

# The minimum field set every trade document extraction must cover.
REQUIRED_FIELDS = [
    "consignee_name",
    "hs_code",
    "port_of_loading",
    "port_of_discharge",
    "incoterms",
    "goods_description",
    "gross_weight_kg",
    "invoice_number",
]

# Below this extractor confidence a field is never trusted: the validator is
# forced (in code, not by LLM discretion) to mark it "uncertain".
CONFIDENCE_FLOOR = 0.70


class FieldValue(BaseModel):
    """One extracted field: value + calibrated confidence, never a bare guess."""
    value: Optional[str] = None          # null = unreadable/absent, stated honestly
    confidence: float = Field(ge=0.0, le=1.0)
    note: Optional[str] = None           # e.g. "digits obscured by smudge"


class ExtractionResult(BaseModel):
    doc_type: str                        # commercial_invoice | bill_of_lading | other
    fields: dict[str, FieldValue]
    overall_quality: str                 # clean | degraded | unreadable
    notes: Optional[str] = None


class CheckStatus(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"


class FieldCheck(BaseModel):
    field: str
    status: CheckStatus
    found: Optional[str] = None
    expected: Optional[str] = None
    reason: str


class ValidationResult(BaseModel):
    customer_id: str
    checks: list[FieldCheck]
    summary: str

    def counts(self) -> dict[str, int]:
        out = {"match": 0, "mismatch": 0, "uncertain": 0}
        for c in self.checks:
            out[c.status.value] += 1
        return out


class Decision(str, Enum):
    AUTO_APPROVE = "auto_approve"
    HUMAN_REVIEW = "human_review"
    AMENDMENT_REQUEST = "amendment_request"


class RoutingDecision(BaseModel):
    decision: Decision
    reasoning: str                       # evidence-grounded explanation, shown in UI + stored
    discrepancies: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class StepMeta(BaseModel):
    """Observability record for one agent step (one row in agent_steps)."""
    agent: str
    status: str                          # ok | error | skipped_resume
    attempts: int = 1
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None     # reported by SDK; informational under subscription auth
    error: Optional[str] = None


class PipelineState(BaseModel):
    """The single state object threaded through the LangGraph graph."""
    run_id: str
    doc_path: str
    customer_id: str = "customer_x"
    extraction: Optional[ExtractionResult] = None
    validation: Optional[ValidationResult] = None
    decision: Optional[RoutingDecision] = None
    step_meta: list[StepMeta] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    failed: bool = False                 # set when an agent exhausts its retry budget


# JSON Schemas handed to the Agent SDK's structured-output constraint.
def extraction_json_schema() -> dict[str, Any]:
    field_schema = {
        "type": "object",
        "properties": {
            "value": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "note": {"type": ["string", "null"]},
        },
        "required": ["value", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string", "enum": ["commercial_invoice", "bill_of_lading", "packing_list", "certificate_of_origin", "other"]},
            "fields": {
                "type": "object",
                "properties": {f: field_schema for f in REQUIRED_FIELDS},
                "required": REQUIRED_FIELDS,
                "additionalProperties": False,
            },
            "overall_quality": {"type": "string", "enum": ["clean", "degraded", "unreadable"]},
            "notes": {"type": ["string", "null"]},
        },
        "required": ["doc_type", "fields", "overall_quality"],
        "additionalProperties": False,
    }


def validation_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "status": {"type": "string", "enum": ["match", "mismatch", "uncertain"]},
                        "found": {"type": ["string", "null"]},
                        "expected": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["field", "status", "reason"],
                    "additionalProperties": False,
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["checks", "summary"],
        "additionalProperties": False,
    }


def routing_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["auto_approve", "human_review", "amendment_request"]},
            "reasoning": {"type": "string"},
            "discrepancies": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["decision", "reasoning", "discrepancies", "confidence"],
        "additionalProperties": False,
    }
