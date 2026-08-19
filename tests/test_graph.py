"""Graph tests: full run, crash-after-extract simulation, and resume."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                     # noqa: E402
from app.graph import run_pipeline     # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "sample_docs"


async def full_run():
    state = await run_pipeline(str(DOCS / "bill_of_lading_clean.pdf"))
    print("FULL RUN:", state.run_id, "->", state.decision.decision.value)
    for m in state.step_meta:
        print(f"  {m.agent:10s} {m.status:15s} {m.latency_ms}ms in={m.input_tokens} out={m.output_tokens} cost={m.cost_usd}")


async def crash_run():
    """Run only until the extractor persists, then hard-exit mid-pipeline."""
    import app.graph as g

    orig = g.validate_node

    async def bomb(state):
        print("CRASH: killing process before validator runs (extractor output is in SQLite)")
        os._exit(1)

    g.GRAPH = None
    g.validate_node = bomb
    g.GRAPH = g.build_graph()
    await run_pipeline(str(DOCS / "commercial_invoice_clean.pdf"), run_id="crashdemo01")
    g.validate_node = orig


async def resume_run():
    state = await run_pipeline("", run_id="crashdemo01", resume=True)
    print("RESUMED RUN:", state.run_id, "->", state.decision.decision.value)
    for m in state.step_meta:
        print(f"  {m.agent:10s} {m.status}")
    rec = db.get_run("crashdemo01")
    print("DB status:", rec["run"]["status"], "| steps:", [(s["agent"], s["status"]) for s in rec["steps"]])


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    asyncio.run({"full": full_run, "crash": crash_run, "resume": resume_run}[mode]())
