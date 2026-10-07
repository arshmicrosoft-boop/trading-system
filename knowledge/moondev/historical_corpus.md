# Historical Corpus: What Has Already Been Tried, Not What Is Known to Work

Source repository: Moon Dev's Moon-Dev-Code archive.
Tier: **tier-4** provenance (YouTube / social media). Weak evidentiary weight.

## Purpose

A large archive of publicly documented strategies is easy to mistake for a source of
truth — a library of things known to work, from which one selects. It is not. It is a
*record of attempts*: what was tried, how it was framed, under what assumptions, and how
the author revised it over time. Its value to a research engine is completely different
from its value to a trader.

For us its single most important contribution is **deduplication**. A research engine
without this record rediscovers the same handful of ideas indefinitely — each rediscovery
is stored as a fresh hypothesis, each is queued, each consumes a Layer 2 experiment slot,
and each arrives with no awareness that its twin failed three weeks earlier. The corpus is
what lets the critic answer "has this already been explored?" before an idea costs anything.
That makes the corpus a **novelty and negative-knowledge device**, not a strategy library.

What this document is emphatically **not**: a list of strategies that work. See
[Anti-pattern rule](#the-anti-pattern-rule-read-this-before-citing-anything-in-the-corpus)
below.

Related: [RBI methodology](rbi_methodology.md) ·
[AI agent architecture](ai_agent_architecture.md) ·
[Controlled experiment design](controlled_experiment_design.md)

Related knowledge: [contradiction resolution](../methods/contradiction_resolution.md) ·
[hypothesis testing](../methods/hypothesis_testing.md) ·
[research integrity checklist](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) ·
[frameworks: qlib](../frameworks/qlib.md),
[finrobot](../frameworks/finrobot.md),
[tradingagents](../frameworks/tradingagents.md)

## Its role in the pipeline: a deduplication index, not a source of truth

Three different questions, and three different correct behaviours:

| Question | Correct behaviour against this corpus |
|---|---|
| Has this idea already been explored? | Check the corpus **before** drafting. If yes, the new hypothesis is `not_distinct`. |
| What variation was tested, and with what assumptions? | Quote the documented variation and its assumptions as the thing that makes it a distinct claim — or as evidence that the variation space is already crowded. |
| What did the author change over time, and why? | Treat revision history as a signal about which parameters are *decision-relevant* and which framing failed. A strategy whose author repeatedly changed the same parameter was asking a harder question than it appeared. |

This is a `sources`-side input, ingested as `source_type = 'youtube'`, `tier = '4'`
([`db/schema.sql`](../../db/schema.sql)). It enters the pipeline through mechanism
extraction like any other source. It carries no special privilege in the pipeline: it is
evidence, it gets an assigned tier, and tier affects how much weight its claims can bear —
not whether they are considered.

## Indexing metadata

Each corpus entry is indexed with the fields needed to answer the three questions above
without re-reading the video. The schema below is what the engine needs; a future agent
adding entries should not omit any of it.

```yaml
source:                 moon-dev-code/<slug>       # where it lives in the archive
video:                  <url>                      # canonical link to the item
date:                   <yyyy-mm-dd>               # publication or recording date
strategy_family:        <bounded family label>     # e.g. order_flow, momentum, carry
market_mechanism:       <the economic mechanism>    # why it should work, in one claim
features:               [<observable variables>]    # what it actually needs computed
claimed_rationale:      <author's stated reasoning> # recorded as CLAIMED, not verified
backtest_methodology:   <method, period, costs>    # or an explicit "not documented"
known_limitations:      [<author-acknowledged>]     # or "none stated"
```

Two fields deserve emphasis because they are the ones usually skipped:

- **`backtest_methodology` must be explicit about absence.** If the entry does not document
  costs, splits, or survivorship, record `"not documented"` — do not leave the field blank
  and do not infer a method. A blank field reads as "unknown but probably fine"; an
  explicit absence reads as what it is, which is a much weaker evidentiary basis.
- **`claimed_rationale` is stored as a claim, not a finding.** It records what the author
  said the mechanism was. Whether that mechanism is plausible is decided downstream, by the
  critic, on the mechanism's merits — not inherited from the fact that someone published it.

## Deduplication: why this changes the critic's behaviour

Without the corpus indexed, `distinct` is unanswerable. The critic has a dimension
`no_prior_failure_collision` and a failure type `prior_failure_precedent`, and both require
a searchable record of what came before — including what was merely *proposed elsewhere*,
which is exactly what the corpus is. The corpus populates the memory the distinctness check
reads.

With it indexed:

- A new hypothesis whose mechanism and features match an existing one is `not_distinct` and
  never reaches the queue.
- A new hypothesis that refines an existing one (narrower universe, different horizon,
  different trigger) is a legitimate *refinement* — but the difference must be named and
  the mutation must be declared, using the taxonomy in
  [`hypothesis_discovery_loop.md`](../research_integrity/hypothesis_discovery_loop.md).
  "Same idea, slightly tweaked" is not a new idea and must not be allowed to consume an
  experiment slot as if it were.
- An idea already refuted in *our* pipeline is caught by `prior_failure_precedent`
  against the `failures` table instead, without needing the corpus at all. See
  [`failure_modes.md`](../methods/failure_modes.md) and
  [`REPLICATION.md`](../methods/REPLICATION.md) for the replication shape, which is the
  legitimate exception: re-testing a known failure is allowed **if and only if** it is
  declared as a replication with its reason for existing.

This is the practical value: the corpus converts *repetition* from an invisible cost into a
cheap, early check. Novelty is checked before cost is incurred, rather than discovered when
the queue is full of near-duplicates.

## Tier-4 provenance: how much weight this carries

**This corpus is tier-4 evidence: social-media / YouTube provenance.** Its claims sit two
tiers below practitioner-grade writing and four below peer review. In
[`db/schema.sql`](../../db/schema.sql), `source_tier` is explicitly described as *provenance
weight only; never a pass/fail gate* — which cuts both ways:

- Tier-4 material may be ingested, extracted from, and used to *generate questions*.
- Tier-4 material may **never** be cited as support for a hypothesis's expected market
  behaviour, may never be the sole justification for a mechanism, and may never stand in
  for evidence in a formalized spec's provenance chain.

And the distinction that matters most:

> **A documented experiment is not a validated result.**

Seeing a strategy written up, even with charts and even with a claimed backtest, tells you
an experiment was *described*. It does not tell you the experiment was controlled, costed,
or replicated. Given the survey figures in
[controlled experiment design](controlled_experiment_design.md) — of 19 primary studies
only 2 used time-consistent splits, 1 had a cost model, and 1 documented survivorship — the
prior that a publicly shown backtest is properly controlled should be treated as low unless
the entry's `backtest_methodology` field says otherwise. Most entries will not say
otherwise. That is the expected state, not a reason to distrust the corpus as a *record* —
only as a basis for a *claim*.

## The anti-pattern rule: read this before citing anything in the corpus

> **Never transform "Moon Dev used X" into "X works."**

This rule and this corpus are the combination that makes it necessary. The corpus is
large, specific, and gives a strong feeling of empirical grounding — it reads like a body of
results. It is a body of *descriptions*. The inference from "this was tried and written up"
to "this works" requires evidence the corpus does not contain: costs, splits, universe
definition, out-of-sample testing, and replication.

The failure mode is concrete and, in our pipeline, one-directional: the corpus contains
enough concrete strategies that a mechanism extractor will produce hypotheses that look
well-motivated *because* they resemble documented practice. The motivation is borrowed. A
hypothesis must be motivated by its mechanism, and the corpus may only tell you that the
idea is not novel.

For the same reason we reject any return target or profitability claim being attached to
corpus entries — see the full rejection in
[AI agent architecture](ai_agent_architecture.md). No field in the indexing metadata above
is a performance number, and none should be added.

## What we deliberately did not copy, and why

- **The strategy code itself.** We index *descriptions*, not implementations. Pulling in
  someone else's executable strategy would let an unfalsifiable artefact enter the pipeline
  with a working implementation attached — the exact shape
  [`HYPOTHESIS_SCHEMA.md`](../methods/HYPOTHESIS_SCHEMA.md) and the `no_hidden_discretion`
  critic dimension exist to prevent. Our pipeline produces its own formal specs.
- **The corpus as a recommendation or ranking.** No ordering, no "best", no top-N. Ordering
  is a performance judgement and this corpus cannot support one.
- **Backtest results shown in the archive as evidence.** Recorded in
  `backtest_methodology` as a *claim about the author's method*, never imported into our
  results or used as a prior.
- **The archive's implicit status.** Being published by a successful practitioner does not
  confer evidentiary weight. Tier 4 is tier 4.
- **Everything about a profitability target.** Same rejection as elsewhere: a research layer
  that can see one becomes a metric-gaming machine.

## Epistemic status

**Tier-4 provenance.** The archive's contents and the shape of its entries were read from
the repository itself; the indexing metadata block above is *our proposed* schema for
extracting from it, not a schema the repository already uses, and it has not yet been
validated against the full archive — expect fields that cannot be populated for many
entries, particularly `backtest_methodology` and `known_limitations`.

A future agent must treat every entry as: *a documented attempt at tier-4 provenance*. It is
valid input for novelty detection, invalid input for evidence of a market effect. If a
hypothesis's only support is that something similar appears in this corpus, that hypothesis
should fail the critic — the corpus answers "is this new?", not "is this true?".