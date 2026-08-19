# PRD — Trade Document Extraction, Validation & Routing Pipeline

*Part 1 of the Nova Day-At-Work assignment. The pipeline this PRD describes is
built and running — see README.md to reproduce every claim in here.*

---

## 1. Understanding Nova, the FDE role, and the System of Outcomes

### 1.1 What Nova is

Nova is GoComet's attempt to rebuild their logistics software around
configurable process and governed intelligence, instead of around a fixed set
of screens. There are four pieces to it, as I understand them. The Workflow
Orchestrator turns multi-step business processes into configuration: YAML
underneath, a React Flow graph editor on top, so a client can define their own
approval chain instead of engineering hardcoding it. The Agents Orchestrator is
where the AI actually lives: agents doing document extraction, validation,
monitoring, and recommendations, running through what the JD calls a
five-stage pipeline (scope resolution, context compilation, schema routing,
plan and execute, evidence delivery). The No-Code App Builder covers the cases
where a workflow step needs more than an approve or reject button, like a rate
comparison panel. And the Data Layer pulls everything into ClickHouse,
tenant-isolated, so it's queryable later.

What ties these together, as far as I can tell, is that the agents are allowed
to make real decisions, not just surface information, but every decision has
to be scoped to a tenant, backed by evidence, and auditable after the fact.
That's a different bet than most "AI feature" products make. My Part 1 build is
a small, single-tenant version of the same idea: three agents with a narrow job
each, and every decision stored with the reasoning behind it.

### 1.2 What the FDE role is

A Forward Deployed Engineer at GoComet owns one client's workflow from the
first discovery call through to production, and stays accountable for whether
it actually works, not just whether it shipped. The AOE model pairs one
engineer with one client partner, described as "2-in-a-box," which tells me
this isn't a role where someone hands you a spec. You're in the room when an
operator says "validate the invoice" and it turns out that actually means
eleven customer-specific rules, three of which quietly contradict the written
SOP.

The engineering implication I tried to build into this POC is that
customer-specific logic has to live in configuration, not in a code branch,
because the next FDE onboarding a new client needs to write a rules file in an
afternoon, not open a pull request. That's why I put the validation rules in
`rules/customer_x.yaml` instead of hardcoding them into the validator agent.

It also means failure has to be explainable in the language an operator uses,
not engineering language. "The HS code was smudged, so a human checks it" is a
sentence an FDE can say out loud on a client call. "Confidence score fell below
threshold" is not, even if it's the same fact.

### 1.3 System of Outcomes

I think about it as three different things being measured. A System of Record
is judged by whether the data is correct: did the TMS know the shipment's ETA
changed. A System of Engagement is judged by usage: did the operator open the
dashboard, did they see the flagged invoice. A System of Outcomes is judged by
something further downstream than either of those: did the business result
actually happen. Not "was the discrepancy displayed," but "was the amendment
sent, did the shipment clear, did we avoid the demurrage fee."

You can see the distinction in how the router agent behaves. A System of
Record would just store the extracted fields. A System of Engagement would
show a human the mismatch and stop there, waiting for a click. This pipeline
instead decides: it auto-approves the clean invoice and drafts the actual
amendment request for the Rotterdam versus Hamburg mismatch, and it keeps the
evidence trail so that decision can be checked later.

It's also why the metrics in this PRD are outcome metrics, touchless approval
rate and exception resolution time, rather than engagement metrics like logins
or page views. If a human has to re-check every field anyway, the agent added
cost without adding an outcome, and the whole premise falls apart.

---

## 2. Problem statement

Every ocean/air shipment produces a document stack — Bill of Lading, Commercial
Invoice, Packing List, Certificate of Origin — issued by different parties as
PDFs and scans of wildly varying quality. Today a human reads every field of
every document and validates it against customer-specific expectations that
mostly live in operators' heads.

Specific failure modes in the manual flow:

- **Late discovery.** A wrong discharge port or HS code is caught at customs or
  at the carrier, not at document receipt — when fixing it costs demurrage,
  storage, re-filing fees, and days of delay instead of one amendment email.
- **Silent guessing.** Under time pressure, a smudged HS code gets typed in as a
  "best guess". Nobody records that it was a guess, so downstream systems treat
  it as fact.
- **Tribal-knowledge validation.** "Customer X only ships CIF or FOB and always
  via Hamburg" exists in one senior operator's memory. New hires either don't
  know or re-ask.
- **Uniform effort.** The 80% of documents that are perfectly fine consume the
  same reading time as the 20% with problems, so throughput scales only with
  headcount.
- **No queryable trail.** "How many of this supplier's invoices had HS-code
  problems last quarter?" means opening PDFs one by one.

**A CG operator's first 5 minutes of success** *(assumption flagged: I read "CG"
as the GoComet-side operations user and "SU" as the supplier-side user — verify
the acronyms)*: they open one screen, see the morning's documents already
extracted, validated and routed; the clean ones marked auto-approved so they can
ignore them; one invoice flagged with "discharge port shows Rotterdam, Customer X
requires Hamburg — amendment drafted" and a pre-written discrepancy list they can
send to the supplier after a 30-second sanity check. They handled only the
exception, they can see *why* it was flagged without opening the PDF, and they
trust the "why" because it cites found-vs-expected values, per-field.

## 3. Personas & Jobs-To-Be-Done

**Persona A — CG operator (Priya, documentation/ops executive at the freight
buyer/forwarder).** Processes 40–80 trade documents a day against customer SOPs.
Measured on clearance delays and error escapes. Deeply skeptical of automation
that hides its reasoning, because she is accountable when it's wrong.

**Persona B — SU supplier (Chen, export documentation clerk at the shipper).**
Issues invoices and packing lists against purchase-order terms. Wants to know
about discrepancies immediately and precisely — a vague "documents rejected"
email costs him a day of back-and-forth; a field-level amendment list costs him
ten minutes.

JTBD:

1. When a new document stack lands in my queue, I want the routine ones
   pre-validated and auto-approved, so that I spend my attention only on
   exceptions.
2. When the system flags a document, I want to see exactly which field failed,
   what was found vs. expected, and why, so that I can act in seconds without
   re-reading the PDF or re-deriving the rule.
3. When a scan is too poor to read a field reliably, I want the system to say
   "unreadable" rather than guess, so that no silent error enters downstream
   customs filings under my name.
4. When a discrepancy is confirmed, I want an amendment request drafted with
   every discrepancy itemized (as the SU supplier), so that I can correct and
   reissue documents in one cycle instead of three.
5. When my manager asks "how many shipments got flagged this week and for what,"
   I want to ask the system in plain English and get an answer grounded in real
   records, so that reporting doesn't mean opening PDFs one by one.
6. When I onboard a new customer's rules (as the FDE/ops admin), I want to encode
   them as configuration rather than requesting a code change, so that setup
   takes an afternoon, not a sprint.

## 4. Agent architecture

### Why 3 agents — not 1, not 5

One mega-agent ("read this PDF and tell me approve/reject") conflates three
different failure domains, and when it's wrong you can't tell whether extraction,
rule interpretation, or judgment failed — which kills both debuggability and the
audit story. Splitting also lets each stage run with the *minimum* capability it
needs: the extractor is the only agent with any tool access (`Read`, nothing
else); the validator and router run tool-less with `max_turns=1`, so their attack
and failure surface is a single constrained completion.

Five agents (e.g., separate doc-classifier, per-field confidence scorer, or a
distinct amendment-drafter) is where I drew the line the other way: doc-type
classification falls out of extraction for free; confidence scoring belongs
*inside* extraction (the model knows what it could read while looking at the
pixels — a downstream agent would be guessing); amendment drafting is one field
of the router's output, not a stage. Each extra hop is +5–15s latency, +$0.02–0.04,
and one more schema boundary to break. Three agents = one per failure domain:
**perception** (what does the document say?), **judgment against rules** (is it
acceptable for this customer?), **decision** (what happens next?).

### Responsibilities, I/O, handoffs

| Agent | Input | Output (Pydantic) | Tools / turns | Guarded by |
|---|---|---|---|---|
| Extractor | doc path (PDF/PNG) | `ExtractionResult`: 8 fields × `{value, confidence, note}`, doc_type, quality | `Read` only, ≤4 turns | never-guess prompt contract; JSON schema; retry cap |
| Validator | `ExtractionResult` + `rules/<customer>.yaml` | `ValidationResult`: per-field `match/mismatch/uncertain` + found/expected/reason | none, 1 turn | code forces `uncertain` for any field below 0.70 confidence or null; skipped required fields re-added by policy |
| Router | `ValidationResult` | `RoutingDecision`: decision + evidence-grounded reasoning + discrepancy list | none, 1 turn | code computes the *allowed* decision set (any mismatch ⇒ never auto-approve); LLM choice outside it is overridden and the override is recorded |

Handoffs are typed Pydantic objects flowing through a LangGraph `StateGraph` —
no raw text is ever passed between agents. The shared `PipelineState` carries
`run_id`, the three stage outputs, per-step telemetry, and error/budget flags.

### Mapping to Nova's five-stage pipeline (where it genuinely fits)

- **Scope resolution** ≈ `run_id` + `customer_id` binding at run creation — thin here (one tenant, one doc); honest to say this POC barely exercises it.
- **Context compilation** ≈ loading the customer rule set + extraction into the validator's prompt.
- **Schema routing** ≈ selecting `rules/customer_x.yaml` by customer — in production this is choosing which extraction schema and rule profile apply per doc-type × customer.
- **Plan + execute** ≈ the LangGraph graph itself.
- **Evidence delivery** ≈ the router's stored reasoning + per-field found-vs-expected trail, rendered in the UI and queryable in SQL.

The two ends of the mapping (scope resolution, and "plan" as a distinct step —
this graph's plan is static) are the forced fits, and I'd rather say so than
pretend a three-node DAG is a planner.

### How state survives a crash

Every node persists its output to SQLite (`agent_steps.output_json`, plus
normalized `extracted_fields` / `field_checks` rows) the moment it completes, and
every node is idempotent — if its output already exists in state, it records
`skipped_resume` and passes through. Resuming = rehydrating `PipelineState` from
SQLite by `run_id` and re-invoking the same graph. Verified by hard-killing the
process (`os._exit`) after extraction: the resume run skipped the extractor (no
re-paid LLM call), completed validate+route, and the run finished `completed`.

## 5. LLM & tooling choices, with tradeoffs

- **Claude Agent SDK over the raw `anthropic` SDK.** Chosen to run the whole POC
  on a subscription **Claude Code OAuth token** (`claude setup-token` →
  `CLAUDE_CODE_OAUTH_TOKEN`) — zero marginal API spend for a personal build, and
  the SDK's `Read` tool gives PDF/image ingestion for free. Cost of the choice:
  it's an agent harness, not a bare completion endpoint, so determinism had to be
  imposed — tools stripped to none (except the extractor's `Read`), `max_turns`
  capped, `setting_sources=[]` so local Claude config can't leak in, and native
  `output_format` JSON-schema constraining every response, re-validated by
  Pydantic. Notable: the brief anticipated the tool-loop fighting structured
  output; with the SDK's native structured output it never did — no agent needed
  a workaround. **Scope note:** a personal OAuth token is legitimate for a
  personal local build, but not for a hosted multi-client product; production
  moves to metered API keys / enterprise billing (LiteLLM-style routing +
  budgets), at which point the token/cost telemetry this POC already logs
  converts directly into dollar figures.
- **Model: Claude Sonnet everywhere.** Measured on the sample docs: extraction
  quality identical to bigger models (correctly nulled the smudged HS code,
  transcribed everything legible), ~$0.08–0.10 and ~30–40s per document
  all-in. Vision fallback strategy: (1) retry once — transient vision misreads
  are real; (2) below the 0.70 confidence floor the *pipeline itself* is the
  fallback (field → `uncertain` → human, never a guess); (3) config-level model
  escalation per agent in `app/llm.py` (e.g. re-run `degraded`-quality docs on
  Opus; not enabled by default because the messy sample didn't need it); (4) a
  production option this POC skips: a dedicated OCR/DPT-2-style pass feeding text
  alongside the image.
- **LangGraph** for orchestration: the graph is three nodes and could be three
  `await`s — but the explicit `StateGraph` gives typed state flow, conditional
  failure edges, and the same mental model as Nova's production Agents
  Orchestrator, which is the point of the exercise. Overhead measured: negligible
  vs. the LLM calls.
- **Structured output used**: all four LLM call sites (3 agents + NL→SQL), all
  schema-constrained. **Avoided**: nowhere — the only free text in the system is
  *inside* schema fields explicitly meant for humans (reasons, reasoning,
  answer), which is where LLM prose belongs.
- **SQLite over ClickHouse**: one tenant, hundreds of rows, zero setup — SQLite
  is honest for a laptop demo. The schema (runs / agent_steps / extracted_fields
  / field_checks) is deliberately the shape you'd land in ClickHouse when
  multi-tenant analytical volume justifies it. **Structured JSONL logs +
  `run_id` threading over Langfuse/OTel**: same tracing intent, one dependency
  fewer; the swap-in path is in the technical write-up. **No Kafka**: Part 1's
  trigger is a direct upload, not an event stream — Debezium/Kafka enter when
  ingestion becomes email listeners and carrier feeds.
- **Plain HTML/JS UI over React+Vite**: one screen rendering real backend state;
  a build step buys nothing here (brief explicitly allows this).

## 6. Trust & failure handling

- **Hallucination prevention:** extraction values must be transcriptions (prompt
  contract: null + low confidence + note when unreadable — observed working: the
  smudged HS code came back `null`/0.20 with "digits obscured by smudge", not an
  invented `8471.xx`). Validator/router only see structured upstream output, and
  their prose must cite found-vs-expected values. NL answers are generated from
  executed SQL rows included in the prompt, with the SQL exposed in the UI for
  audit.
- **Low-confidence handling:** a code-enforced 0.70 floor — below it a field
  cannot be `match` regardless of what the validator model says; the demotion is
  appended to the check's reason so the override itself is auditable.
- **Loop/cost guards:** ≤2 attempts per agent, 180s per-call timeout, hard 8-call
  budget per run (`CallBudget`), all logged when hit — the run fails loudly as
  `failed`, never hangs. This is the POC stand-in for LiteLLM-style budget
  control.
- **Deterministic decision governance:** the allowed-decision set is computed in
  code from validation counts; the model cannot auto-approve past a mismatch.
- **One offline eval I'd actually run:** a golden set of ~50 documents (clean,
  degraded, adversarial) with human-labeled field values and expected decisions;
  every prompt/model/rule change replays it and reports field-level extraction
  accuracy, calibration (confidence vs. correctness), and decision agreement.
  The three sample docs in this repo are the seed of exactly that set.
- **One online metric I'd actually run:** human override rate on routed decisions
  — how often an operator reverses an auto-approve (critical, should be ~0) or
  releases a flagged doc without changes (noise, target <20%). It directly
  measures whether the routing is trustworthy, and it generates labeled training
  data as a side effect.

## 7. Metrics

**North star: touchless-and-correct rate** — % of documents that flow
extract→validate→route→auto-approve with no human touch *and* no subsequent
override/escape. (Touchless alone incentivizes reckless approval; the "correct"
clause keeps it honest.)

Supporting:
1. Field-level extraction accuracy on the golden set (target ≥97% on clean docs, ≥90% on degraded).
2. Confidence calibration: correctness rate of fields scored ≥0.9 (target ≥98% — if 0.9 means 80%, the floor is meaningless).
3. False-approve rate: approved docs later found discrepant (target ~0; this is the error that costs demurrage).
4. False-flag rate: flagged docs a human releases unchanged (target <20%).
5. Median end-to-end latency per document (POC actual: ~30–40s; target <2 min including queue).
6. Cost per document (POC actual: ~$0.08–0.10 at metered-API prices).
7. Exception resolution time: flag → amendment sent (target: minutes, vs. day-scale today).
8. Crash-resume integrity: resumed runs completing without re-running finished steps (POC: verified; production: monitored).

**Go/no-go for a 2-week single-customer pilot (shadow mode — system decides,
humans still process everything):** GO to assisted mode if decision agreement
≥90%, false-approve = 0 across the pilot volume, extraction accuracy ≥95% on the
customer's real docs, cost/latency within 2× targets, and the operator says the
flag reasons are actionable without opening the PDF. NO-GO triggers regardless of
averages: any silent hallucinated field value that survived to a decision, or
false-approves on fields the customer calls critical (HS code, ports, consignee).

## 8. Next 2 weeks

1. **Multi-document reconciliation** (highest value): validate the invoice
   *against* the B/L and packing list for the same shipment — cross-doc weight,
   HS, consignee mismatches are where the expensive errors live. The state
   schema already keys by shipment-able fields (invoice_number).
2. **Golden-set eval harness in CI** (§6) — nothing else can be changed safely
   without it.
3. **Email/watch-folder ingestion + queueing**, replacing manual upload — the
   real trigger, and the first place backpressure and dedup matter.
4. **Rule-set editor UI** on top of the YAML (the no-code direction), so an FDE
   or ops admin onboards a customer without touching the repo.
5. **Close the gap toward the production stack where scale justifies it:**
   Langfuse/OTel tracing replacing JSONL logs (the `run_id` threading is already
   trace-shaped), ClickHouse behind the same table shapes when volume demands
   it, OpenFGA-style tenant scoping on every row and every rule-set read, and
   metered API billing with LiteLLM-style routing replacing the personal OAuth
   token — the POC's per-agent token logs become real cost dashboards on day one.
6. **Confidence-driven model escalation** (auto re-extract `degraded` docs on a
   stronger model) — cheap to add, directly lifts the north star.
