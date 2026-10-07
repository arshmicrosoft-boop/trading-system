# Controlled Experiment Design: Holding Everything But the Variable Fixed

Source repository: Moon Dev's AI Trading Battles.
Tier: primary source for **experimental design practice**, not for any trading result.

## Purpose

A "battle" in which AI agents trade the same market under the same rules is, stripped of
the entertainment framing, a statement about experimental hygiene: the only defensible way
to compare two things is to make everything except the thing being compared identical. This
document takes that principle seriously enough to make it a hard architectural rule for
Layer 1, because the failure it prevents — comparisons across uncontrolled conditions — is
not a rare mistake. It is the default outcome of most research pipelines, and it is silent:
the resulting claim sounds exactly as reasonable as one derived from a clean experiment.

What this document is **not** evidence of: that any agent, model, or strategy performed
well in any such battle. It is evidence about *method*. The user has supplied concrete
figures from a survey of 77 studies (19 primary), of which only 2 used time-consistent
splits, 1 included a transaction-cost model, 1 documented survivorship, and 11 reported
execution timing. Those figures are used here as the *motivation* for the metadata contract
below, not as results about markets.

Related: [RBI methodology](rbi_methodology.md) ·
[AI agent architecture](ai_agent_architecture.md) ·
[Historical corpus](historical_corpus.md)

Related knowledge: [hypothesis schema](../methods/HYPOTHESIS_SCHEMA.md) ·
[hypothesis testing](../methods/hypothesis_testing.md) ·
[reproducibility audit](../research_integrity/reproducibility_audit.md) ·
[knowledge leakage](../research_integrity/knowledge_leakage.md) ·
[research integrity checklist](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) ·
[methodology references](../frameworks/methodology_references.md)

## The controlled-comparison principle

For a comparison to support a conclusion, these must be identical across the arms:

- **Market information** — same instruments, same universe, same timestamp resolution.
- **Cadence** — same decision frequency, same rebalance schedule, same clock.
- **Rules** — same entry/exit logic, same thresholds, same horizon.
- **Sizing** — same position sizing, same capital, same leverage convention.
- **Execution framework** — same order type, same fill model, same latency assumption.
- **Cost model** — same fees, same spread assumption, same slippage assumption.

And regardless of comparison:

- **Every decision is logged.** Not the summary. The decision, its inputs, its timestamp.
- **The prompt and harness are auditable.** The model output and the scaffolding around it
  are both inspectable artefacts, not transient state.

Together these mean the only remaining difference between two arms is the variable under
test. That is what makes the difference attributable.

## The rule this yields

> **Control everything except the variable under test.**

Stated with its failure mode, because the failure mode is the point:

> **Two hypotheses compared on different data, costs, or periods are not comparable, and
> treating them as comparable is the most common way research lies to itself.**

This is not a hypothetical. The survey figures make the case concretely: of 19 primary
studies, only 2 used time-consistent splits and only 1 included a transaction-cost model. A
result obtained without a cost model and a result obtained with one are answers to
different questions; placing them in the same table and ranking them is a category error
that produces a confident, entirely spurious ordering. The same applies to survivorship (1
of 19 documented it) and to execution timing (only 11 of 19 reported it) — a strategy
evaluated at close prices and a strategy evaluated at the next available fill are
measuring different objects.

The rule has a direct architectural consequence for us: **uncontrolled variables must be
recorded at the moment the claim is made, not reconstructed later.** By the time someone
wants to know which costs a past result assumed, that information is gone. Hence the
metadata contract below.

## The experiment metadata contract we adopted

Every queued experiment carries the full tuple that makes a Layer 2 result reproducible
months later. Layer 1 writes all of these at enqueue time; Layer 2 inherits them as
conditions of the experiment, not as defaults it may adjust.

```yaml
# research_identity / experiment_queue identity fields
hypothesis_id:          hyp_<content_minted_id>   # the falsifiable claim under test
data_version:           dv_<yyyymmdd>_<content_hash>   # exact dataset snapshot
universe:               crypto_perp              # instruments + market, fixed
time_period:            2023-01-01..2024-12-31    # and the split structure within it
feature_version:        fv_<hash>                # ordered feature list, frozen
execution_assumptions:                          # free-form but mandatory block
  order_type:           market
  fill_model:           next_bar_open
  decision_latency_ms:  0
  participation_cap:    0.05
cost_model:
  fee_bps:              4.0                       # taker fee both sides
  half_spread_bps:      1.5                       # charged on every fill
  slippage_model:       fixed_bps
  slippage_bps:         2.0
random_seed:            1729                      # every stochastic step, including splits
model:                  <model_used>              # which model generated/parameterised this
prompt_version:         <prompt_version>          # which prompt produced the spec
```

Two properties of this block are load-bearing:

- **It is complete or the experiment does not exist.** `data_version`, `feature_version`,
  `random_seed`, `model` and `prompt_version` are `NOT NULL` in
  [`db/schema.sql`](../../db/schema.sql). A claim whose conditions cannot be stated is
  rejected at the critic (`not_measurable`) rather than queued with blanks.
- **It is frozen at enqueue.** The `experiment_queue_immutable_identity` trigger rejects
  any UPDATE that changes `experiment_id`, `research_id`, `hypothesis_id`, `version_id`,
  `feature_set`, `rule_version`, `data_version`, `parameter_space`, or `queued_at`. Layer 2
  may not adjust the conditions of the test it was handed. If a condition genuinely needs to
  change, that is a *different* experiment with a different identity.

`random_seed` deserves a specific note: it is included so that any stochastic component —
parameter sampling, splits, resampling — is repeatable. An experiment that cannot be
re-run identically is an anecdote.

## Multidimensional evaluation: no scalar "score"

The battle benchmark ranks agents on **risk-adjusted performance and drawdown**, not on
raw dollars. Raw profit is the most easily gamed and least informative axis available: it
rewards position size, leverage and a single lucky regime, none of which are properties of
an idea. Drawdown and risk-adjusted measures are what remain when those are controlled away.

Our architectural commitment, which follows directly:

> **We will NEVER store a single scalar called `strategy_score`.**

A single scalar is a rank-ordering device, and rank-ordering devices are exactly where an
irreducible amount of judgement gets laundered into apparent objectivity. Worse, the
compounding of a scalar across *research-quality* dimensions reproduces the flaw we already
rejected in [`ai_agent_architecture.md`](ai_agent_architecture.md): give the generator one
number to optimise and it will optimise the number. The dimensions are kept separate and
individually inspectable:

| Dimension | Question |
|---|---|
| mechanism plausibility | Is there an economic reason for this to work? |
| measurable | Can it be computed from data we actually have? |
| falsifiable | Is there an observation that would refute it? |
| distinct | Has this already been tried? See [historical corpus](historical_corpus.md) |
| no hidden discretion | Can two people implement it identically from the spec? |
| no future leakage | Does any input postdate the decision it informs? |
| no prior failure collision | Has this already been refuted here? See [failure modes](../methods/failure_modes.md) |

The verdict is `PASS`/`FAIL` over those dimensions, not a weighted sum. The `failures`
table records *which* dimension failed and why, so that a rejected hypothesis leaves behind
a usable reason rather than a number.

## How this enables a real experiment later

The metadata contract is not bookkeeping — it is what makes a genuine scientific experiment
possible within our own pipeline. The experiment we are building toward:

> **Same source, same mechanism, same formalized spec, same data version, same cost model,
> same seed — varying only `model` (and secondarily `prompt_version`) — to study whether
> model choice changes research quality.**

Under the current design that experiment is *structurally* possible: all the control
variables above are already stored per experiment, and `model` / `prompt_version` are
already stored per hypothesis. So we can ask, months later and from data rather than from
memory:

- Does the model generate more hypotheses that survive the critic?
- Does it do so by being more *distinct* (see
  [hypothesis discovery loop](../research_integrity/hypothesis_discovery_loop.md) for the
  mutation taxonomy that defines distinctness), or merely by recombining?
- Does it produce mechanisms that are more plausible, or more elaborate?
- Does the quality difference persist across source tiers — i.e. is the effect about the
  model or about how the source was chosen?

None of these questions can be answered without the metadata contract. None of them can be
answered by a return target, because a return target would answer a different and worse
question: *which model generates the prettiest backtests*, which is precisely the
metric-gaming failure documented in [AI agent architecture](ai_agent_architecture.md).

## What we deliberately did not copy, and why

- **The comparative/competitive framing.** Trading battles are entertaining and the
  adversarial setup is a reasonable way to hold constants fixed, but an arena whose output
  is a leaderboard invites optimising the leaderboard. We kept the controlled-comparison
  discipline and dropped the contest.
- **Any implied performance target for a winner.** We adopted the multidimensional
  evaluation axes; we did not adopt a threshold value. In particular we did not adopt
  `TARGET_RETURN = 50` or `SAVE_IF_OVER_RETURN = 1%` — see
  [AI agent architecture](ai_agent_architecture.md) for the full rejection.
- **Their storage of results.** Their results live in whatever the run wrote out. Ours live
  in an append-only database keyed by a content-minted identity, because a result that
  cannot be joined back to its conditions is not evidence.
- **Implicit cost assumptions.** The survey figures (1 of 19 with a transaction-cost model,
  1 of 19 documenting survivorship) describe the field, not this repository; we treated them
  as the reason to make `cost_model` a mandatory block rather than as a critique of a
  specific implementation we have not read line by line.

## Epistemic status

Primary source for the controlled-comparison discipline. The orchestration and evaluation
axes were read from the repository itself; the survey figures (77 studies / 19 primary,
2 time-consistent splits, 1 cost model, 1 survivorship, 11 execution-timing) are
**user-supplied** from research notes and were not independently verified here. Use them as
motivation for the metadata contract and for how seriously to treat the checklist in
[`RESEARCH_INTEGRITY_CHECKLIST.md`](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md),
not as verified statistics.

Normative for our architecture. This document asserts no profitability for any strategy and
states no expected return — and its central claim is that a research layer which states one
is measuring the wrong thing.