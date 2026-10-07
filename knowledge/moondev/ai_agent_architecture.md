# AI Agent Architecture: Orchestration Worth Stealing, and One Constant Worth Rejecting

Source repository: Moon Dev's AI agent system.
Tier: primary source for **system design**, not for any trading outcome.

## Purpose

Moon Dev's agent repo is interesting to us for one reason and dangerous for another. The
reason: it is a working multi-agent research pipeline, and its shape — generate ideas,
gather material from many external sources, fan out, produce many independent research
outputs — is the shape our research engine wants. The danger: that pipeline is wired to a
profitability constant, and copying its shape without removing that constant reproduces the
failure mode it creates.

This document exists so that an engineer building Layer 1 can take the orchestration and
leave the constant behind. The orchestration is reusable; the scoring target is an
architectural defect.

What this document is **not** evidence of: that Moon Dev's agents find profitable strategies,
that agent-driven research works better than manual research, or that the multi-agent
fan-out improves anything at all. Fan-out improves *throughput of idea production*. Those
are not the same claim, and only the first is supported here.

Related: [RBI methodology](rbi_methodology.md) ·
[Controlled experiment design](controlled_experiment_design.md) ·
[Historical corpus](historical_corpus.md)

Related knowledge: [hypothesis schema](../methods/HYPOTHESIS_SCHEMA.md) ·
[failure modes](../methods/failure_modes.md) ·
[replication](../methods/REPLICATION.md) ·
[hypothesis discovery loop](../research_integrity/hypothesis_discovery_loop.md) ·
[reproducibility audit](../research_integrity/reproducibility_audit.md) ·
[frameworks: qlib](../frameworks/qlib.md),
[finrobot](../frameworks/finrobot.md),
[tradingagents](../frameworks/tradingagents.md)

## The orchestration worth stealing

The pipeline as described:

```
Research Agent
  -> ideas
Web / Search Agent
  -> research material gathered externally, across multiple sources
Strategy Agent
  -> strategy ideas derived from that material
Parallel RBI
  -> many Research/Backtest/Implement runs fanned out from the ideas
```

Three structural moves in that chain are worth taking:

1. **Idea generation and evidence gathering are different agents.** The Research Agent
   produces candidate directions without reading external sources; the Web/Search Agent
   produces material without deciding what it is for. Separating them prevents the
   generator from anchoring on whatever material happened to be easy to retrieve, which is
   the most common way a research pipeline quietly reduces to "re-derive what search
   surfaced first".
2. **Material becomes ideas through a distinct step.** A source is not a strategy. An
   explicit derivation step (mechanism extraction) is where an agent turns evidence into a
   claim, and it is the step at which provenance can be attached.
3. **The work fans out after the ideas exist, not before.** Cheap serial work (idea
   generation, source collection) happens once; expensive parallel work (per-hypothesis
   research) happens many times. Fan-out before the fan-out-able unit is well-defined is
   wasted compute.

The user reports a parallel implementation of this shape with **18 workers spanning multiple
data sources**. Treat that number as user-supplied context about the reference system, not
as a benchmark and not as a target. There is no requirement that our system use any
particular worker count; the design constraint is on what workers may disagree about.

## Translating the worker pattern into our terms: a queue, not a swarm

The failure mode of "18 workers" architectures is not slowness. It is that parallel workers
each accumulate local context and drift into incompatible conventions — two agents
describing the same mechanism with different variable names, three different definitions of
"volume regime", and no mechanism by which the disagreement is ever detected. Throughput
goes up and *epistemic consistency* goes down. That is a net loss: the output is a corpus
of artefacts that cannot be compared, so no critic, no ranking, and no dedup can be applied
to it.

Our translation:

> **A research QUEUE, drained by workers, all writing into ONE canonical database.**

Concretely, this is why our pipeline is `sources → mechanisms → hypotheses → critique →
formal spec → INTEGRITY CHECK → experiment_queue → Layer 2` rather than a graph of agents
passing free text. The queue is the coordination point, the database is the single shared
state, and workers are stateless with respect to each other.

The rule this yields:

> **Parallelism must increase throughput, never create competing standards of "research".**

Two supporting decisions follow, and both are already enforced in
[`db/schema.sql`](../../db/schema.sql):

- **Enums over free text for anything categorical.** Domain, status, failure type, source
  tier and source type are native enums, so the database cannot accept an unparseable
  category that a downstream stage would have to defend against. Free-text taxonomies are
  the mechanism by which N workers produce N vocabularies.
- **One identity per claim.** A worker cannot invent a hypothesis identity; it is minted by
  [`layer1/ids.py`](../../layer1/ids.py) from content, so two workers producing the same
  claim produce the *same row*, not two rows that later have to be reconciled.

## What we explicitly refuse to copy: the return target

The reference implementation contains `TARGET_RETURN = 50` and `SAVE_IF_OVER_RETURN = 1%`
— a profitability threshold and a "keep this one" rule gated on beating it.

We do not copy either. This is not a stylistic disagreement; here is the concrete failure
mode.

**The mechanism by which a visible target corrupts research.** A return target is an
optimisation objective placed *upstream of evidence exists*. A hypothesis generator that
knows "the target is 50%" does not ask "what is true about this market?"; it asks "what
kind of claim tends to produce a backtest that scores well against that number?" Those are
different searches, and the second one reliably wins because it is optimised directly. The
output is a research layer that has become a metric-shaped text generator. Every artefact it
produces is well-formed, plausible, falsifiable-looking — and selected for flattering a
number rather than for being true.

Three concrete symptoms, all of which we would then have to fight:

- **Confirmation-by-construction.** The generator proposes claims whose measurable
  consequence is favourable under the target metric, and the *falsifier* it writes is the
  weak one. A hypothesis paired with a rigged null passes a critic that only checks
  falsifiability.
- **Threshold-shaped parameter selection.** `SAVE_IF_OVER_RETURN = 1%` makes retention
  depend on the metric, so the surviving population is selected on outcome. At research
  time those outcomes do not exist, so in practice the constant either does nothing or is
  satisfied by whatever the generator was told would satisfy it — either way it is not a
  quality filter.
- **Research that cannot fail.** If hypotheses are generated under a target and retained
  against it, the surviving set is by construction the flattering set. Layer 2 then
  "confirms" them. The whole pipeline becomes unfalsifiable while looking rigorous.

**Our layer must be structurally incapable of this.** This is a design requirement, not a
convention:

- `RuleSpec` sets `model_config = extra='forbid'` and validates that an `expected_return`
  field cannot be represented ([`layer1/contracts.py`](../../layer1/contracts.py)). The
  target is not forbidden by policy; it is unrepresentable.
- `research_identity` has no expected-return column, and the schema comment says so
  explicitly.
- Every hypothesis must state `hypothesis.null` — the observation that would refute it.
  A hypothesis that cannot be refuted is rejected as `not_falsifiable`, regardless of how
  attractive it sounds.
- Layer 1 never writes a result into `experiment_queue`. It can only enqueue. The only
  place a performance judgement is permitted to exist in this system is Layer 2, which
  Layer 1 cannot influence and does not share a process with.

**Layer 1's only question is "is this well-defined and motivated enough to deserve testing?"**
It answers that question using research-quality dimensions only: mechanism clarity,
falsifiability, measurability, distinctness, absence of hidden discretion, absence of future
information leakage, absence of prior-failure collision. Not one of those is a trading
performance dimension. If a future change would let Layer 1 see a return target, that change
is an architecture regression regardless of what it claims to improve.

## Our pipeline as a corrected version of theirs

| Reference system | Ours | Why |
|---|---|---|
| Research Agent free-associates on TA indicators | Extractor works inside bounded [domain universes](../../layer1/domains.py) | Free association produces combinatorially many shallow, incomparable claims. See [RBI methodology](rbi_methodology.md). |
| Ideas pass between agents as prose | Every stage writes a typed row: sources → research_notes → hypotheses → hypothesis_versions | Free text between agents is where competing standards of research grow. |
| Sources consumed, then discarded | `sources` is append-only, content-hashed, tier-assigned, UPDATE blocked by trigger | Provenance must survive the process that read it. |
| 18 workers, each with local context | Queue drained by workers into one database; enums and content-minted identities prevent vocabulary drift | Parallelism must increase throughput, never create competing standards of research. |
| `TARGET_RETURN = 50`, `SAVE_IF_OVER_RETURN = 1%` | No return target is representable anywhere in Layer 1 | See the failure mode above. |
| "Keep the winner" | `failures` is a first-class table with a `stage` column; a rejection is a result | A pipeline that only emits winners cannot learn what it should stop proposing. |
| Backtest decides what to keep | Layer 1 admits or rejects on research quality only; Layer 2 owns every performance judgement | See [Controlled experiment design](controlled_experiment_design.md). |

## What we deliberately did not copy, and why

- **`TARGET_RETURN = 50` and `SAVE_IF_OVER_RETURN = 1%`.** Detailed above. They turn a
  research stage into a target-optimising stage, and they make retention depend on a metric
  that does not exist yet at the moment of research. Rejected structurally, not by
  convention.
- **Their notion of "a good strategy result" as the thing the research loop optimises for.**
  We took the loop's *ordering* and discarded its *objective*.
- **Free-form agent-to-agent handoff.** Adopted the fan-out; rejected the implicit
  conventions that only exist in one agent's context window.
- **Whatever worker count the reference system uses.** 18 workers is user-supplied
  context about their system, not a specification for ours. The number is a deployment
  decision; the single-canonical-store rule is not.
- **Any claim that agent fan-out improves research quality.** We have not verified that,
  and our schema must not assume it. Fan-out is assumed to increase *production rate of
  candidates*, nothing more. Whether candidates are any good is what the critic and Layer 2
  are for.

## Epistemic status

Primary source for Moon Dev's agent architecture. The orchestration sequence and the
constants `TARGET_RETURN = 50` / `SAVE_IF_OVER_RETURN = 1%` come from reading the
repository itself (project structure, agent modules, and the constants as they appear in
code). The **18-worker / multi-data-source figure is user-supplied** and was not independently
verified here; do not quote it as a measured property of their system.

Read this document as normative architecture guidance for our pipeline. It is not evidence
that Moon Dev's strategies or agent outputs are profitable, and it states no expected return
for any strategy — deliberately, because the rejection of `TARGET_RETURN` is the point.