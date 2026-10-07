# Methodology references — source index, tiers and discovery protocol

## Purpose

This is the **index document** for the reference corpus: what each external source
contributes to our architecture, what epistemic standing it has, and the protocol for
turning a pointer into evidence.

It exists because the corpus has a failure mode specific to LLM-assisted research: a
long chain of documents citing each other, none of which trace back to anything a human
read. This document makes that chain auditable. Every source named here is classified,
and the classification says what the source may be used *for*.

It is **not** a source of research findings. Nothing in this file is evidence about any
market effect, and no performance number appears anywhere in it.

## The governing rule

> **SOURCE QUALITY IS NOT CLAIM TRUTH.**

A prestigious source can still contain an unverified, untested or outright wrong
hypothesis. Peer review establishes that a claim was competently presented, not that it
is true. An official repository establishes that the author wrote the code, not that the
design is correct. An educational video establishes that the concept is teachable, not
that it holds in a market.

Therefore: **a source's tier sets how much the source's framing is trusted; it never
converts a claim inside it into a fact.** Tier is applied to *authority over its own
subject matter* — an author's standing on what they built, a reviewer's standing on
whether a study was well conducted. A T1 peer-reviewed paper containing an unbacked
mechanism assertion is still an unbacked mechanism assertion, and the mechanism must be
extracted as a *hypothesis to be tested*, never as a *finding to be assumed*.

Our `sources.tier` column encodes the same discipline and the schema says so explicitly:
"Provenance weight only; never a pass/fail gate." Tier never gates a hypothesis. It
shapes how the evidence is read.

## The tier hierarchy (T0–T6)

This extends the operational five-tier scale our schema stores (`sources.tier`, an enum
of `'1'`–`'5'`: peer-reviewed, practitioner, blog, YouTube, LLM-generated) with two
tiers below and above it that matter for architecture and for honesty about our own
output. The schema's five are the ones a *stored source* can actually have; T0 and T6
are the bookends that bound the scale.

| Tier | Meaning | Typical example | How it may be used |
| --- | --- | --- | --- |
| **T0** | Primary empirical / raw experiment | Our own experiment output; a raw dataset with provenance | The only tier that can settle a question about *our* system |
| **T1** | Peer-reviewed | Refereed journal or conference paper | Methodological template; findings are candidate mechanisms, not facts |
| **T2** | Author's technical repository | The qlib, FinRobot, TradingAgents, FinGPT repos | Authoritative about the authors' own design; evidence about nothing else |
| **T3** | Practitioner research | Sell-side, desks, independent write-ups with a stated method | Hypothesis generation; plausible mechanisms, unverified |
| **T4** | Educational / video | YouTube, courses, tutorials | Vocabulary and intuition only; never a mechanism's sole justification |
| **T5** | Secondary summary | Blog aggregations, listicles, "12 papers you must read" | **Discovery only.** A pointer to T1–T3, never the evidence itself |
| **T6** | LLM-generated | Our own extraction output, model assertions, this corpus's inferences | Requires external verification before it informs anything |

T6 deserves emphasis because it includes this document. A structured knowledge document
produced by an LLM is **T6**: its architectural claims are sound only where they are
corroborated by code we can read (as in [finrobot.md](finrobot.md), where the claims are
verified against `layer1/contracts.py` and `db/schema.sql`), and its claims about
external projects are unverified restatements of prior reading.

**Note on our stored scale:** `source_collector.py` assigns a default tier by source
type — `pdf`=1, `article`=3, `youtube`=4, `github`/`note`/`manual`=2 — on the reasoning
that the *medium* predicts likely standing but does not guarantee it. This is a default,
overridable, and it encodes a T5-equivalent risk: a high-tier PDF can be junk, and a
Tier-4 video can contain the only careful measurement of a mechanism we care about. The
hierarchy above exists to keep that mapping honest.

## The discovery protocol (for awesome-lists and every T5 pointer)

> **An awesome-list description leads to a primary source; the primary source is then analysed. Never treat a list's description as truth.**

This is a hard rule, not a preference. A list entry is a *catalogue record*: it asserts
that a project exists and summarises what someone thought it did at the time of writing.
Both halves decay — repos get abandoned, descriptions get copied and embellished down
chains of mirrors, and a one-line summary cannot represent an integrity caveat buried in
a paper's appendix. If a mechanism enters our corpus from a list description, the list
description is T5 and the mechanism is unsupported.

Operationally: discover at T5, retrieve at T1–T3, analyse from the primary artefact, and
record in `research_notes` the artefact actually read — not the list entry that led to
it.

## The priority table (S0 / S1 / S2)

Priority expresses **how load-bearing a source is for our architecture**, which is
independent of evidentiary tier. A T2 repository can be S0 (architecturally decisive) and
a T1 paper can be S2 (worth knowing, changes nothing).

### S0 — architecturally decisive; a future contributor must read before changing structure

| Source | Contributes | Tier |
| --- | --- | --- |
| Moon Dev RBI methodology | The Research → Backtest → Implement split; why Layer 1 ends at the queue; the profitability target we deliberately reject | T3 / T2 (repo + author's own write-ups) |
| [qlib.md](qlib.md) | Modularity as the justification for our component and table boundaries; formal signal languages | T2 |
| [finrobot.md](finrobot.md) | Models reason / software computes / agents orchestrate / systems verify; enforcement in code | T2 |
| [tradingagents.md](tradingagents.md) | Point-in-time evidence; publication date and availability as mandatory claim context | T2 + established data-engineering principle |
| Internal research-integrity corpus | Leakage, reproducibility, the mutation taxonomy | T0 (our own audits) |

### S1 — materially informs hypothesis quality; read before generating mechanisms

| Source | Contributes | Tier |
| --- | --- | --- |
| [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) | The operative gate before enqueue | T0 (our own) |
| [../methods/HYPOTHESIS_SCHEMA.md](../methods/HYPOTHESIS_SCHEMA.md) | What a formalized hypothesis must express, including its null | T0 (our own) |
| [../methods/failure_modes.md](../methods/failure_modes.md) | Typed rejection vocabulary behind `failure_type` | T0 (our own) |
| bettyguo/awesome-llm-trading-agents | Discovery index **for integrity concerns specifically** (see below) | T5 |
| LLMQuant / awesome-trading-agents | Discovery index for tooling and agent patterns | T5 |
| Large Language Model Agent in Financial Trading (arXiv 2408.06361) | The **taxonomy** of the field, for gap identification | T1 preprint |

### S2 — background; informs vocabulary and breadth, changes no decision

| Source | Contributes | Tier |
| --- | --- | --- |
| FinGPT | Background on financial-domain language models | T2 |
| General LLM-agent literature | Vocabulary for orchestration patterns already adopted via [finrobot.md](finrobot.md) | T1/T2 |

## Source-by-source notes

### FinGPT — background only, explicitly NOT a core dependency

FinGPT is a foundation-model effort for financial language. Its contribution to us is
**background**: it tells us what a financially-tuned language model is, which helps when
reasoning about model choice for the extractor and generator.

We are building a **research system, not a financial foundation model**. Pre-training is
out of scope, and nothing in our pipeline depends on a domain-tuned checkpoint: our
`llm.py` wraps any provider behind a `Provider` protocol with `OllamaProvider`,
`OpenAIProvider` and `DeterministicProvider` implementations, so the model is a
configuration choice, not an architectural commitment.

The trap to avoid is *scope inflation by association*: because FinGPT is a flagship
financial-LLM project, it is tempting to treat it as the foundation of a "financial
agent system". That would replace a tractable research-engineering problem with a
model-training programme, and would put the entire system's quality under the control of
a dataset we would have to assemble. We take the background and decline the dependency.

### LLMQuant / awesome-trading-agents and bettyguo/awesome-llm-trading-agents — discovery layers

Both are **discovery layers, not factual authority**. They are T5 under the governing
rule above: they tell us what exists, and nothing more. Use them to find the primary
artefact; analyse that.

`bettyguo/awesome-llm-trading-agents` is distinctive and worth singling out: its
methodology section explicitly discusses **look-ahead bias, backtest overfitting,
multiple testing, survivorship bias, gross-vs-net returns, and live-vs-backtest
discrepancy**. That makes it a *discovery index for integrity concerns specifically* —
a place to discover that a given community is worried about a specific failure mode,
after which the actual primary literature on that failure mode is retrieved and read.

This is precisely the right way to use it and precisely the wrong way to use it wrong.
Right: notice that multiple testing is a recognised concern, then find and read the
primary methodological literature. Wrong: record "multiple testing is a problem" *on the
authority of the list*, and let a T5 entry supply a critique dimension that a real
source should. Our own critique dimensions already cover this ground structurally —
`CritiqueDimension` includes `mechanism_plausible`, `measurable`, `falsifiable`,
`distinct`, `no_hidden_discretion`, `no_future_leakage` and
`no_prior_failure_collision`, and `failure_type` includes `not_falsifiable`,
`future_information_leak` and `prior_failure_precedent`. Those came from primary
reasoning about the failure modes, and a list entry cannot add to them without a primary
source behind it.

### Large Language Model Agent in Financial Trading (arXiv 2408.06361) — a taxonomy, not a strategy

Its value to us is **structural, not substantive**. We are not adopting any strategy,
result or number from it. Its contribution is a clean partition of the field along two
axes, which we use to ask *what have we not covered?*

**DATA** — what the agent reads:
price data; fundamentals; news; sentiment; social media; macro variables; order book
data; alternative data.

**REASONING** — how the agent thinks:
single agent; multi-agent; debate; reflection; memory; tool use.

**ACTION** — what the agent is permitted to do:
prediction; signal generation; portfolio construction; execution.

#### How we use the taxonomy against ourselves

Its purpose for us is **gap identification** — a checklist for auditing our own coverage,
not a catalogue to copy:

- **DATA axis → our gap is explicit and known.** `ALL_VARIABLES` in `layer1/domains.py`
  spans order book / microstructure (OFI, microprice, imbalance, depth, spread, trade
  imbalance, aggressive volume, queue dynamics), forced flows (liquidations, open
  interest, funding, basis, liquidation clusters), relative value (cross-market, basis
  spreads, lead/lag) and price/volume (momentum, mean reversion, volatility, breakouts,
  volume regimes). **We have no fundamentals, no news, no sentiment, no social, and no
  macro.** That is a deliberate scope boundary — Layer 1 studies market-structure
  mechanisms — but naming the omission against the taxonomy is what stops it from
  becoming an unnoticed hole. The taxonomy is the check that the hole is *chosen*.
- **REASONING axis → we take single-agent-per-stage plus one critic.** Our stages are
  single agents, not a multi-agent debate, a reflection loop or a memory system
  (see [finrobot.md](finrobot.md) for why we declined debate-for-its-own-sake). What
  survives the taxonomy's scrutiny is not "agents are good" but "each reasoning step
  needs a typed, checkable output" — our stages differ by *output contract*, which is
  the axis the taxonomy does not cover and the one that matters for us.
- **ACTION axis → the sharpest finding.** The taxonomy's third bucket is
  prediction / signal generation / portfolio construction / execution. **Our Layer 1
  occupies none of it.** Signal generation is Layer 2's; portfolio construction and
  execution have no concept in our schema at all. This makes the boundary legible
  against an external framework rather than only against our own intent, which is the
  most useful thing this source does for us.

The taxonomy's value is thus *diagnostic*: read against our pipeline it immediately shows
which cells are empty by design, and a future contributor with a new idea in one of
those cells now knows they are proposing a scope change, not a bug fix.

## Direct consequences for Layer 1's architecture

1. **Extraction must record the artefact read, not the pointer followed.** Every
   `research_notes` row is anchored to a `sources` row carrying `url`, `date` and
   `content_sha256`. Discovery intermediates are not a thing we store as evidence.
2. **Tier is recorded and non-gating.** `sources.tier` is a native enum so it cannot
   hold a value Layer 2 would have to interpret, and it informs reading posture only —
   never admission to the queue.
3. **Extraction is constrained at the prompt.** `extractor.py`'s system prompt embeds
   `domains.describe_domains()` and hard-forbids trade recommendations, buy/sell, entry/
   exit, position sizing and profitability claims. This is the taxonomy's DATA axis turned
   into an enforced boundary: the agent is told which universes are in scope, and the
   architecture holds it there.
4. **The gap list is written down, so it stays a choice.** The DATA-axis omissions
   (fundamentals, news, sentiment, social, macro) should appear in our design docs as
   *named exclusions*. A gap nobody wrote down is a gap nobody is guarding.
5. **Every T6 claim in this corpus is marked.** Following the governing rule, documents in
   this repository carry an epistemic-status section, and claims that could not be
   verified against code are labelled as coming from supplied research notes.

## What we deliberately did NOT copy, and why

- **No strategy, benchmark or performance figure from any source in this document.**
  Including figures would import exactly the contamination this corpus exists to prevent:
  a Layer 1 that can see profitability expectations will generate hypotheses tuned to
  flatter a backtest. Layer 1's question is "is this well-defined and motivated enough
  to deserve testing?" — and a target number in context makes that question rhetorical.
- **No awesome-list as a knowledge source.** Discovery only. No `research_notes` row may
  cite a list entry as its `source_id`.
- **No foundation-model dependency.** Per the FinGPT note: providers are swappable
  configuration.
- **No strategy backfilling from the survey.** The taxonomy's ACTION axis tells us what
  Layer 2 will do. It does not make any of it Layer 1's work.

## Epistemic status

**Mixed, and honestly labelled per entry.** The tier table, the governing rule and the
discovery protocol are **our own methodological decisions** (T0-equivalent: derivable
from the corpus's own requirements, no external authority needed). The descriptions of
FinGPT, LLMQuant, bettyguo's list, and arXiv 2408.06361 — including the survey's
taxonomy and the bettyguo methodology-section topics — come from **the user's supplied
research notes**, not from my reading of those sources in this session. I did not fetch
arXiv 2408.06361, did not browse either awesome-list, and did not read the FinGPT
repository.

Consequently:

- **Verified in this session**: every statement about our own code and schema (the tier
  enum values and their documented meanings, the `DEFAULT_TIER_BY_TYPE` mapping, the
  `CritiqueDimension` and `FailureType` vocabularies, the extractor's prompt
  constraints, the provider classes, the domain variable universes). These are T0/T6-
  corroborated-by-code.
- **User-supplied, unverified**: the four-source descriptions and the taxonomy. They are
  recorded at T5-equivalent standing *about those external projects* until a future agent
  retrieves the primary artefacts. No statistics, quotations, or file paths are asserted
  anywhere in this document, precisely because I could not verify them.

A future agent should treat the taxonomy as a **working scaffold for gap identification**
and should retrieve the actual survey before citing it in any research output. If a
claim here is found wrong, correct it in place rather than propagating it — this is a T6
document and T6 is not a terminal tier.

## Related

- [qlib.md](qlib.md) · [finrobot.md](finrobot.md) · [tradingagents.md](tradingagents.md) — the S0 architecturally-decisive sources, detailed
- [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md) — the S0 source we depart from, and the return target we reject
- [../moondev/historical_corpus.md](../moondev/historical_corpus.md) — the source of the profitability-target convention we deliberately do not inherit
- [../methods/failure_modes.md](../methods/failure_modes.md) — the critique vocabulary that a T5 list entry cannot extend
- [../methods/hypothesis_testing.md](../methods/hypothesis_testing.md) — how a T1 finding becomes a hypothesis rather than an assumption
- [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) — the operative checklist this index feeds
- [../research_integrity/knowledge_leakage.md](../research_integrity/knowledge_leakage.md) — the concrete failure this corpus is built to prevent