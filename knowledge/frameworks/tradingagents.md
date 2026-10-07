# TradingAgents — point-in-time evidence, and where we stop

## Purpose

Tauric Research's TradingAgents contributes two things: a decomposition of trading work
across specialised agents, and — more valuable to us — a body of **integrity work** on
temporal correctness of evidence. Its recent work targets point-in-time integrity,
as-filed fundamentals, look-ahead fixes, decision logs, and checkpoint/resume.

For our purposes the agent decomposition is context and the integrity work is the
substance. It is **not** evidence that any of its strategies work. TradingAgents is a
framework: its shipped results are the output of its own backtester under its own
assumptions, which we cannot audit from a repository description.

## Key concepts

### Multi-specialised-agent decomposition

TradingAgents splits a trading session across distinct agent roles — research, analysis,
decision and backtesting — each with its own prompt, its own inputs and its own output,
coordinated by a manager. The gain is not intelligence; it is that each role's output can
be inspected and challenged independently, and a failure is attributable to a named role.

The limitation for us is structural rather than stylistic: two of those roles (decision,
execution) act on markets. Our Layer 1 has no market-facing concept at all.

### Point-in-time integrity

This is the part we adopt. The core observation is that a value and its *availability*
are different facts. Retrieving `revenue = X` must also retrieve `published_at`,
`period_end` and `available_to_market_at`, because:

- `published_at` — when the document was released.
- `period_end` — the close of the fiscal period the figure describes.
- `available_to_market_at` — the earliest moment a market participant could have acted on it.

These are three distinct dates and they are routinely unequal. A figure for a quarter
ending 31 March may be published on 30 April. Any dataset that stores only `revenue` and
a single `date` has silently chosen one of them, and if it chose `period_end` it has
manufactured look-ahead bias across every historical backtest that touched it: the model
appears to have known the number before the number existed.

TradingAgents' look-ahead fixes and as-filed fundamentals work is the operational form
of this principle — take fundamentals as they were filed, when they were filed, not as
currently restated.

### Decision logs and checkpoint/resume

Recording what was decided at each step, and resuming from a checkpoint rather than
re-running, make a long agent pipeline auditable and reproducible. Both address a
failure mode where a partially-completed run is indistinguishable from a run that
never started.

## Direct consequences for Layer 1's architecture

### THE PRINCIPLE WE ADOPT: point-in-time evidence, generalised into the source model

Generalised past fundamentals, the rule is: **a claim is not retrievable without its
temporal context, and the temporal context is part of the claim, not metadata about it.**

Our `sources` table already carries two of these dates, and they are non-negotiable
columns rather than optional fields:

- `sources.date` — `TIMESTAMPTZ NOT NULL`, the source's own date, `publication_date` in
  our vocabulary.
- `sources.ingested_at` — `TIMESTAMPTZ NOT NULL DEFAULT now()`, the moment the evidence
  entered our system, i.e. `available_to_market_at` from the perspective of this system.

`date` is `NOT NULL` with no default, so a source cannot be stored without declaring
when it was published. That is the enforcement: temporal context is unrepresentable-incomplete,
matching the `extra="forbid"` argument in [finrobot.md](finrobot.md). The schema also uses
`TIMESTAMPTZ`, not `TIMESTAMP`, precisely because these timestamps are compared across
machines and against market data, and a naive timestamp silently corrupts those
comparisons.

**A claim inherits the temporal context of its source.** `research_notes` carries a
mandatory `source_id REFERENCES sources(source_id)`, so every mechanism points at exactly
one dated, hashed, immutable piece of evidence, and `hypotheses` in turn references exactly
one `mechanism_id`. The chain source -> mechanism -> hypothesis is therefore a chain of
*dated* provenance, and no hypothesis can float free of a publication date.

Three concrete consequences:

1. **Deduplication is content-addressed, so re-ingestion cannot silently refresh a
   source's content.** `content_sha256` is the dedupe key and the tamper-evidence field.
   Combined with the UPDATE-blocking trigger, the text a claim was extracted from cannot
   change underneath it. A "corrected" source is a *new* source with a *new* date — which
   is the honest representation, because a correction published later genuinely was not
   available earlier.
2. **Temporal context travels with the identity tuple.** `research_identity` requires
   `source_ids` (as a `TEXT[]` with a GIN index) plus `data_version`. A reproducible
   result therefore names *which versions of which dated sources* produced it. If the
   underlying evidence is revised, the identity changes and the old result stays
   interpretable rather than being silently reinterpreted.
3. **Look-ahead is a typed failure, not an oversight.** The `failures` table carries
   `future_information_leak` as a first-class `failure_type`, so a hypothesis whose
   evidence outruns its own availability is recorded as a rejection with a name, not
   quietly tested and discarded. This is the same discipline as the integrity gate
   described in
   [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md).

### What we deliberately did NOT copy, and why

- **Not the decision or execution agents.** This is the stopping line. Decision and
  execution agents belong to Layer 2; Layer 1 has no order, position, fill, balance or
  credential concept anywhere in its schema. Adopting them would not make Layer 1
  better — it would dissolve the boundary that makes Layer 1 auditable. Our "agent
  decomposition" stops at: interpret, generate, formalize, critique.
- **Not the fundamentals-specific machinery.** `published_at` / `period_end` /
  `available_to_market_at` matter most for quarterly corporate filings. Our evidence base
  is papers, repositories, articles and notes about crypto microstructure, where the
  binding constraint is usually the **availability of market data and the publication
  lag of the source itself**. We keep the general rule — every claim carries its
  temporal context — and drop the three-way fiscal-calendar decomposition, which would be
  dead weight for our domain.
- **Not decision logs or checkpoint/resume as new subsystems.** We already have a
  stronger form of both: immutable append-only records with content hashes instead of
  mutable logs, and `research_identity` instead of a resumed session. Adding a checkpoint
  mechanism would duplicate guarantees the schema already makes structurally.
- **Not TradingAgents' strategies or reported performance.** Its results come from its
  own backtester. Those numbers are not evidence for any hypothesis of ours.

## Epistemic status

**Secondary, architectural.** The point-in-time framing — three distinct dates for a
single fundamental, and the look-ahead bias that conflating them creates — is
established financial-data engineering knowledge, and the description of TradingAgents'
specific integrity work (as-filed fundamentals, look-ahead fixes, decision logs,
checkpoint/resume) comes from the user's supplied research notes. I did not read the
TradingAgents repository, code, or commit history in this session. No version numbers,
dates, or issue references are asserted.

A future agent should treat the **three-date distinction itself as solid** (it is a
property of financial data, not a claim about this framework) while treating any
statement about TradingAgents' implementation as **T2-level** — strong evidence about
how its authors approached integrity, zero evidence about markets. A future agent must
also note that adopting TradingAgents' integrity *work* does not make our system
point-in-time-complete: we have no market-data layer, so the harder half of point-in-time
correctness — never reading a bar or a derived feature before it existed — is Layer 2's
responsibility, and our contribution is ensuring the *evidence side* is dated.

## Related

- [qlib.md](qlib.md) — modularity; our agent-per-stage split derives from the same instinct
- [finrobot.md](finrobot.md) — enforcement in code, not in prose
- [methodology_references.md](methodology_references.md) — the source tier hierarchy and the survey taxonomy
- [../research_integrity/knowledge_leakage.md](../research_integrity/knowledge_leakage.md) — how temporal leakage actually enters a research loop
- [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) — the pre-enqueue gate that `future_information_leak` feeds
- [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md) — the mutation taxonomy used when one hypothesis refines another
- [../methods/contradiction_resolution.md](../methods/contradiction_resolution.md) — what to do when two dated sources disagree
- [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md) — why Layer 1 stops before Implement