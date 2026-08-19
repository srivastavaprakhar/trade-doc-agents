"""Agent 1 — Extractor.

Vision extraction of a trade document (PDF or image) into ExtractionResult.
The agent gets exactly one tool (Read) so it can look at the file, a tight turn
cap, and a JSON-schema-constrained output. The system prompt's core contract:
if a field is unreadable, return null + low confidence — never guess.
"""

from pathlib import Path

from ..llm import CallBudget, call_agent
from ..schemas import ExtractionResult, StepMeta, extraction_json_schema

SYSTEM_PROMPT = """You are a trade-document extraction agent for a logistics platform.
You read ONE document (Commercial Invoice, Bill of Lading, Packing List, or Certificate
of Origin) with the Read tool and extract a fixed set of fields.

Rules — these are hard requirements:
1. NEVER guess. If a field is absent, smudged, cut off, or ambiguous, set its value to
   null, set confidence at or below 0.4, and explain in the field's note.
2. Confidence must be calibrated: 0.95+ only for crisply printed, unambiguous text;
   0.7-0.9 for readable-but-degraded text; below 0.5 whenever you had to infer anything.
3. Values are transcribed from the document, not normalized away: keep codes (e.g.
   "DEHAM", "INNSA") if printed. gross_weight_kg is the numeric weight in kg as a string
   (e.g. "1240.00").
4. incoterms is the printed term incl. named place if present (e.g. "CIF Hamburg").
5. If the document is a Bill of Lading, invoice_number is the referenced invoice number
   if printed, else null.
6. Respond ONLY via the structured output schema."""


async def run_extractor(run_id: str, doc_path: str, budget: CallBudget) -> tuple[ExtractionResult, StepMeta]:
    path = Path(doc_path).resolve()
    if not path.exists():
        raise FileNotFoundError(doc_path)
    prompt = (
        f"Read the trade document at {path} and extract all required fields. "
        "Then respond via the structured output schema only."
    )
    data, meta = await call_agent(
        run_id=run_id,
        agent="extractor",
        system_prompt=SYSTEM_PROMPT,
        prompt=prompt,
        json_schema=extraction_json_schema(),
        budget=budget,
        allowed_tools=["Read"],
        max_turns=4,          # read (possibly 2 pages) + answer
        model="sonnet",
    )
    return ExtractionResult.model_validate(data), meta
