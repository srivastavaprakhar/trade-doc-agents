"""Smoke test: one no-tool structured call + one Read-tool vision call."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import CallBudget, call_agent  # noqa: E402


async def main():
    budget = CallBudget()

    # 1. no-tool reasoning call
    data, meta = await call_agent(
        run_id="smoke-1",
        agent="smoke_reasoner",
        system_prompt="You classify Incoterms. Answer only via the JSON schema.",
        prompt="Is 'CIF Hamburg' a valid Incoterms 2020 usage? Set valid true/false and give a one-line reason.",
        json_schema={
            "type": "object",
            "properties": {"valid": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["valid", "reason"],
            "additionalProperties": False,
        },
        budget=budget,
    )
    print("REASONER:", data, "| tokens", meta.input_tokens, meta.output_tokens, "| ms", meta.latency_ms, "| cost", meta.cost_usd)

    # 2. Read-tool vision call on the messy scan
    img = Path(__file__).resolve().parent.parent / "sample_docs" / "commercial_invoice_messy.png"
    data, meta = await call_agent(
        run_id="smoke-2",
        agent="smoke_vision",
        system_prompt="You read scanned trade documents. Use the Read tool on the given path, then answer only via the JSON schema.",
        prompt=f"Read the document at {img} and report the invoice number and the port of discharge.",
        json_schema={
            "type": "object",
            "properties": {"invoice_number": {"type": ["string", "null"]}, "port_of_discharge": {"type": ["string", "null"]}},
            "required": ["invoice_number", "port_of_discharge"],
            "additionalProperties": False,
        },
        budget=budget,
        allowed_tools=["Read"],
        max_turns=4,
    )
    print("VISION:", data, "| tokens", meta.input_tokens, meta.output_tokens, "| ms", meta.latency_ms, "| cost", meta.cost_usd)


asyncio.run(main())
