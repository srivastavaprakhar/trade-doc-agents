"""Manual agent tests: extract both sample docs, then validate + route each."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.extractor import run_extractor          # noqa: E402
from app.agents.router import run_router                # noqa: E402
from app.agents.validator import run_validator          # noqa: E402
from app.llm import CallBudget                          # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "sample_docs"


async def one(doc: str):
    budget = CallBudget()
    rid = f"test-{Path(doc).stem}"
    print(f"\n{'='*70}\nDOC: {doc}")
    ext, m1 = await run_extractor(rid, str(DOCS / doc), budget)
    for name, fv in ext.fields.items():
        print(f"  {name:20s} conf={fv.confidence:.2f}  {fv.value!r}" + (f"  [{fv.note}]" if fv.note else ""))
    print(f"  quality={ext.overall_quality} doc_type={ext.doc_type}")

    val, m2 = await run_validator(rid, ext, "customer_x", budget)
    for c in val.checks:
        print(f"  {c.field:20s} {c.status.value:9s} found={c.found!r} expected={c.expected!r}")
        print(f"                       reason: {c.reason}")

    dec, m3 = await run_router(rid, val, budget)
    print(f"  DECISION: {dec.decision.value} (confidence {dec.confidence:.2f})")
    print(f"  REASONING: {dec.reasoning}")
    for d in dec.discrepancies:
        print(f"   - {d}")
    print(f"  budget: {budget.used} calls, ${budget.total_cost_usd:.4f}")
    return {"doc": doc, "extraction": ext.model_dump(), "validation": val.model_dump(), "decision": dec.model_dump()}


async def main():
    results = []
    for doc in sys.argv[1:] or ["commercial_invoice_clean.pdf", "commercial_invoice_messy.png"]:
        results.append(await one(doc))
    out = Path(__file__).parent / "agent_test_results.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {out}")


asyncio.run(main())
