# MoonBot Layer 1 — Research Engine

Research → Backtest → Implement, following Moon Dev's RBI architecture.

Layer 1 takes research evidence and turns it into **falsifiable hypotheses**, then
queues them for a Layer 2 backtest engine. It does not execute trades, does not
connect to an exchange, and does not decide what to trade.

> **Don't ask AI what to trade. Ask AI what is worth testing.**

## The boundary

```
        SOURCES                MECHANISMS              HYPOTHESES
   transcripts, papers   →   economic causes   →   falsifiable claims
   repos, articles, notes   not trade signals      not strategies
                                                          ↓
                                            ┌──────────────────────────┐
                                            │   Research Critic (hostile)│
                                            │   plausible? measurable?  │
                                            │   falsifiable? distinct?  │
                                            │   leakage? prior failure? │
                                            └────────────┬─────────────┘
                                              FAIL ─────┴───── PASS
                                          ↓                       ↓
                                   failures table      formal rule spec
                                  (negative knowledge)          ↓
                                                 ┌──────────────────────┐
                                                 │ Integrity Checker    │
                                                 │ protocol complete?   │
                                                 └──────────┬───────────┘
                                                            ↓
                                                  EXPERIMENT QUEUE
                                                            ║
   ══════════════════════════════════════════════════════════════════ LAYER 2
                                                            ║
                                                   BACKTEST ENGINE
```

Nothing downstream of that line is required to generate an idea. Layer 1 never
sees a price, never touches an exchange API, and has no order concept.

## Two architectural positions

**1. Layer 1 has no profitability target.** Moon Dev's implementation optimizes
toward a return target (`TARGET_RETURN = 50`, `SAVE_IF_OVER_RETURN = 1%`). We
reject that deliberately. A research layer that can see a return target becomes a
backtest-gaming machine — it generates hypotheses designed to flatter a metric
rather than to be true. `RuleSpec` sets `extra="forbid"`, so `expected_return` is
structurally *unrepresentable*, not merely discouraged.

**2. AI-generated ≠ research-worthy.** The Research Critic's default posture is
adversarial. It forces a measurable verdict and applies a deterministic floor: a
hypothesis scoring 0 on measurability or future-leakage is failed regardless of
what the model claimed.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Windows
cp .env.example .env                       # add your OpenRouter key

docker run -d --name moonbot-l1 \
  -e POSTGRES_USER=moonbot -e POSTGRES_PASSWORD=moonbot \
  -e POSTGRES_DB=moonbot -p 5433:5432 postgres:16-alpine

.venv/Scripts/python -m uvicorn layer1.api:app --port 8000
```

The schema applies itself on startup and is idempotent.

### Choosing a model

Research models run **hosted via OpenRouter** by default. The pipeline makes
many sequential LLM calls, and a small local model on CPU-only hardware is
both slow and weak — the worst combination for hypothesis quality.

> **`:free` model tiers do not work here.** OpenRouter restricts ids ending in
> `:free` to approved agentic harnesses; they reject direct API calls with
> HTTP 403 even from a valid key. Use a paid id.

```bash
# Check the provider before a long run — catches a zero balance in seconds
# rather than forty minutes in:
.venv/Scripts/python -c "from layer1 import llm; print(llm.get_llm().preflight())"

# Full pipeline with a real model:
L1_LLM_MODE=openrouter python examples/ofi_pipeline.py
```

If hosted credit runs out, the local model remains available:

```bash
L1_LLM_MODE=ollama L1_OLLAMA_MODEL=qwen2.5:3b-instruct python examples/ofi_pipeline.py
```

### End-to-end without a model

```bash
L1_LLM_MODE=deterministic python examples/ofi_pipeline.py
```

Walks the whole pipeline: transcript → source → OFI mechanism → 5 hypotheses →
formalized rules → hostile critique → experiment queue. Deterministic mode is
never selected implicitly and stamps `model_used='deterministic/v1'`, so its
artifacts are permanently identifiable as non-LLM-derived.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/sources` | Ingest evidence (immutable) |
| GET | `/sources` · `/sources/{id}` | Read sources |
| POST | `/research/extract` | Extract mechanisms from a source |
| GET | `/mechanisms` | List mechanisms |
| POST | `/hypotheses/generate` | Generate falsifiable hypotheses |
| GET | `/hypotheses` · `/hypotheses/{id}` | List / read hypotheses |
| POST | `/hypotheses/{id}/formalize` | Produce the machine-readable YAML rule |
| POST | `/hypotheses/{id}/critique` | Hostile review → queue or failure |
| GET | `/experiments/queue` | **Layer 2 consumes this** |
| GET | `/failures` | The negative-knowledge database |
| GET | `/health` | Liveness |

Order matters: **critique requires a formalized spec.** You cannot judge
measurability or future-leakage in an informalized claim.

## Data model

| Table | Holds |
|---|---|
| `sources` | Immutable evidence + SHA-256 + evidentiary tier |
| `research_notes` | Extracted mechanisms and candidate variables |
| `hypotheses` | Falsifiable claims with lifecycle status |
| `hypothesis_versions` | Frozen formalized YAML specs (content-addressed) |
| `research_identity` | The full provenance tuple for every hypothesis |
| `experiment_queue` | The Layer 1 / Layer 2 boundary |
| `failures` | First-class negative knowledge |

Schema enforces its own invariants via triggers: `sources` and
`hypothesis_versions` are append-only, and `experiment_queue` identity columns
freeze at enqueue so Layer 2 can only advance `status` — the record of *what was
tested* cannot drift from *what was queued*.

### The provenance contract

Every queued hypothesis carries the tuple needed to reproduce it later:

```
research_id  hypothesis_id  source_ids  feature_set  rule_version
data_version  parameter_space  creation_timestamp  model_used  prompt_version
```

## Research domains

Explicit universes, not an indicator buffet — so hypotheses are comparable and
individually falsifiable.

- **Microstructure** — OFI, microprice, order-book imbalance, depth, spread, trade imbalance, aggressive volume, queue dynamics
- **Forced flows** — liquidations, open interest, funding basis, liquidation clusters, OI changes
- **Relative value** — cross-market relationships, basis spreads, lead/lag dislocations
- **Price/volume** — momentum, mean reversion, volatility, breakouts, volume regimes

## Knowledge corpus

`knowledge/` holds distilled research knowledge — mechanisms, methodology,
failure modes, evaluation standards. It is structured knowledge the agents
consume, **not** a RAG dump of raw documents, and never a set of trading
instructions.

- `knowledge/moondev/` — RBI methodology, agent architecture, controlled experiment design, historical corpus
- `knowledge/research_integrity/` — the research-integrity checklist, reproducibility audit, knowledge leakage
- `knowledge/frameworks/` — Qlib, FinRobot, TradingAgents, methodology references
- `knowledge/methods/` — failure-mode taxonomy, hypothesis testing, contradiction resolution, replication, hypothesis schema

The recurring rule across all of it: **source quality is not claim truth.** A
prestigious repository is a primary source for its author's *methodology*, not
evidence that any strategy it describes works.

## Layout

```
db/schema.sql                  PostgreSQL DDL (idempotent, trigger-enforced)
db/schema_evidence_ledger.sql  additive: claims, contradictions, audits, quality, generations
layer1/config.py               settings
layer1/contracts.py            Pydantic contracts — the cross-component interface
layer1/contracts_ledger.py     ledger contracts (claims, temporal context)
layer1/domains.py              research universes
layer1/evidence.py             Evidence Ledger — typed claims, contradictions, point-in-time
layer1/ids.py                  sequential identifiers + content-addressed research_id
layer1/integrity.py            Research Integrity Checker — the pre-queue gate
layer1/llm.py                  Ollama / OpenAI-compatible / deterministic providers
layer1/quality.py              research-quality scoring + evolutionary lineage
layer1/db.py                   psycopg3 access
layer1/components/             collector → extractor → generator → formalizer → critic → queueing
layer1/api.py                  FastAPI
docs/LAYER2.md                 the Layer 2 consumption contract
examples/ofi_pipeline.py       end-to-end walkthrough
knowledge/                     distilled research knowledge (see above)
tests/                         guard tests for the boundary and the schema
```

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -q
```

The suite guards the claims that matter: that `expected_return` cannot be
constructed, that the database triggers actually reject what they claim to, that
Layer 2 can claim a queued job, and that formalized specs are idempotent.