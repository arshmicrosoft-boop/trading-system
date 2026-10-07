# Layer 2: the backtest engine contract

Layer 1 ends at a queue entry. This document is the contract for Layer 2 — the
backtest engine — and is the *only* supported way for it to obtain work.

> **The boundary.** Layer 1 never executes a trade, never connects to an
> exchange, and never decides what to trade. If a result is what you want, you
> want Layer 2. Conversely, nothing downstream of the boundary is required to
> generate an idea.

Layer 1 has no notion of a good return. Nothing in this document gives it one,
and the metrics below are **Layer 2's output format**, not a target Layer 1
optimizes toward — see
[Why these are not a target](#why-these-are-not-a-target).

---

## The one call you need

```
GET /experiments/queue?status=queued&limit=100
```

or, against the database directly (recommended for a polling worker — no HTTP
in the hot loop):

```sql
SELECT * FROM experiment_queue_layer2
 WHERE status = 'queued'
 ORDER BY queued_at
 LIMIT 100;
```

Both return the same rows. `experiment_queue_layer2` is a view that joins
`experiment_queue`, `hypotheses`, `hypothesis_versions` and `research_identity`,
so one row carries the rule, the claim, and the provenance of the idea. Layer 2
needs no second query and no knowledge of Layer 1's table layout.

A row appears in this view **only** if the Research Critic returned `PASS`.
`queueing._assert_queueable` is the single chokepoint, and it refuses on two
independent grounds because either alone is defeatable — the status column can
be stale relative to the failure log and vice versa:

1. `hypotheses.status = 'rejected'` — the critic already killed it;
2. a `failures` row at `stage = 'critic'` — a recorded critic FAIL, even if the
   status column was never advanced (a crash between the two writes, or a
   direct insert into `failures` by another tool).

The Research Integrity Checker's `gate_ready()` is defined over the LATEST audit
across all statuses, and returns False when there is no audit at all, so an
unchecked hypothesis never earns the right to consume a backtest. Verify before
you run:

```sql
SELECT experiment_id, hypothesis_id FROM experiment_queue_layer2
 WHERE hypothesis_id NOT IN (SELECT hypothesis_id FROM integrity_gate_ready);
```

An empty result is the invariant. A non-empty one means a hypothesis reached the
queue without a passing integrity audit — treat it as a Layer 1 defect to
report, not as a job to run.

## What one row contains

| Column | Meaning | Who uses it |
|---|---|---|
| `experiment_id` | Queue entry identity (`EXP003`). Unique per enqueue. | Layer 2 claim key |
| `research_id` | Content-addressed provenance identity, `R_<16hex>`. | Result key |
| `hypothesis_id` | `H0001`-style claim id. | Result key |
| `version_id` | Row id in `hypothesis_versions`; the frozen spec revision. | Spec lookup |
| `rule_spec` | The full formalized YAML spec, as JSONB. | **The thing under test** |
| `hypothesis_statement` | The falsifiable claim, in prose. | Report labelling |
| `expected_mechanism` | Why the mechanism should exist, as a causal path. | **Attribution key** |
| `candidate_variables` | Variables the claim names. | Feature sanity check |
| `feature_set` | Concrete variables Layer 2 must compute. | Feature engineering |
| `rule_version` | Which frozen rule revision this job tests. | Reproducibility |
| `data_version` | Dataset pointer as known at enqueue. Frozen — see below. | Reproducibility |
| `parameter_space` | JSONB search space. `{}` means "run as written". | Sweep driver |
| `source_ids` | The evidence this idea came from. | Provenance / re-reading |
| `model_used`, `prompt_version` | Which model and prompt produced the rule. | Provenance |
| `creation_timestamp` | When the research identity was minted. | Provenance |
| `status`, `queued_at`, `claimed_at`, `completed_at` | Lifecycle. | Job control |

### `data_version` is frozen: the pin lives in the result

The sentinel `pending_l2_dataset_selection` is what Layer 1 writes into both
`research_identity.data_version` and `experiment_queue.data_version`. The
`experiment_queue_immutable_identity` trigger rejects **any** `UPDATE` that
changes `data_version`, alongside `experiment_id`, `research_id`,
`hypothesis_id`, `version_id`, `feature_set`, `rule_version`, `parameter_space`
and `queued_at`:

```
ERROR:  experiment_queue identity fields are immutable after enqueue
        (experiment_id=EXP003)
```

This is intentional, and the consequence is not optional: **Layer 2 cannot pin
a dataset on the queue row, and must not try.** The only writable columns are
`status`, `claimed_at` and `completed_at`. The concrete dataset snapshot Layer 2
actually ran on belongs in the Layer 2 result record, keyed by `experiment_id`
and `research_id`.

The sentinel is loud rather than silent, but note carefully what it can and
cannot tell you: because the column is frozen, **every** claimed or completed
row reads `pending_l2_dataset_selection` permanently. So this query is a
progress readout, not an invariant check:

```sql
SELECT status, count(*) FROM experiment_queue_layer2
 WHERE status <> 'queued'
 GROUP BY status;
```

The check that must read zero lives in Layer 2's own result store — every
completed experiment must have a concrete `data_version` on its result record.
Layer 1 cannot enforce that, because Layer 1 does not know what datasets exist.

Note also that `experiment_queue.research_id` is `UNIQUE`, and `research_id` is
a content hash of `(hypothesis_id, spec_sha256)`. So one queue row exists per
*(hypothesis, spec revision)* pair: re-enqueueing the same hypothesis formalized
to the same spec returns the row that already exists rather than creating a
second. **A replication on different data or a different period is therefore
not a second queue row** — it is a second Layer 2 execution whose pinned
`data_version`, `time_period` and `cost_model` differ, recorded against the same
`research_id`. That distinction is what makes the comparison a controlled one.

---

## The controlled-experiment metadata contract

> **CONTROL EVERYTHING EXCEPT THE VARIABLE UNDER TEST.**

Stated with its failure mode, because the failure mode is the point:

> **Two hypotheses compared on different data, costs, or periods are not
> comparable, and treating them as comparable is the most common way research
> lies to itself.**

A comparison is only defensible when these are identical across the arms:
market information (same instruments, same universe, same timestamp
resolution), cadence (same decision frequency, rebalance schedule, clock), rules
(same entry/exit logic, thresholds, horizon), sizing (same capital, leverage
convention), execution framework (same order type, fill model, latency
assumption) and cost model (same fees, spread, slippage). Every decision is
logged — not the summary, the decision, its inputs and its timestamp. Together
these leave exactly one difference between two arms: the thing being compared.
That is what makes the difference attributable.

The architectural consequence is that **uncontrolled variables must be recorded
at the moment the claim is made, not reconstructed later.** By the time someone
wants to know which costs a past result assumed, that information is gone.

### The required block

Every Layer 2 result carries this block. It is the full tuple that makes an
experiment reproducible months later.

```yaml
hypothesis_id:          H0056                  # the falsifiable claim under test
data_version:           dv_20261006_9f3c1a     # exact dataset snapshot, pinned by Layer 2
universe:               crypto_perp            # instruments + market, fixed
time_period:            2023-01-01..2024-12-31 # and the split structure within it
feature_version:        fv_8c1d                # ordered feature list, frozen
execution_assumptions:                         # free-form but mandatory
  order_type:           market
  fill_model:           next_bar_open
  decision_latency_ms:  0
  participation_cap:    0.05
cost_model:
  fee_bps:              4.0                    # taker fee both sides
  half_spread_bps:      1.5                    # charged on every fill
  slippage_model:       fixed_bps
  slippage_bps:         2.0
random_seed:            1729                   # every stochastic step, including splits
model:                  ollama/qwen2.5:3b-instruct
prompt_version:         l1-v1
```

`random_seed` is included so any stochastic component — parameter sampling,
split assignment, bootstrap resampling — is repeatable. An experiment that
cannot be re-run identically is an anecdote.

### Who supplies which field

Most of this block is **Layer 2's to supply**, because Layer 1 does not own
datasets, features or costs. The division is not arbitrary:

| Field | Supplied by | Why |
|---|---|---|
| `hypothesis_id` | Layer 1 (frozen on the queue row) | It is the claim under test. Layer 2 does not get to choose it. |
| `model`, `prompt_version` | Layer 1 (frozen on the queue row) | Provenance of the *rule*. Already stored per hypothesis. |
| `feature_version` | Layer 2, derived from `feature_set` | Layer 1 names variables; it does not compute them. The version hash pins the ordering and the transform. |
| `data_version` | **Layer 2**, replacing the sentinel | Layer 1 has no market data access at all. It cannot know which dataset exists, and refuses to pretend. |
| `universe`, `time_period` | Layer 2 | Instrument list and window are data-availability decisions. |
| `execution_assumptions`, `cost_model` | Layer 2 | Venue mechanics. Layer 1 stores no fee schedule and defines no default, on purpose: a default is a free parameter in disguise. |
| `random_seed` | Layer 2 | The experiment's own machinery. |

Once chosen, these are **conditions of the experiment**, not defaults Layer 2 may
adjust between runs or across arms. If a condition genuinely needs to change,
that is a different experiment with a different identity — not a re-run of this
one.

### The five protocol blocks

The block above is Layer 2's own serialization. The reproducibility audit
frames the same requirement as five blocks that must each be *concrete* — an
identifier, a number, or an enumerated policy, never a description of one:

```yaml
temporal_protocol:
  train:      { start: "YYYY-MM-DD", end: "YYYY-MM-DD" }
  validation: { start: "YYYY-MM-DD", end: "YYYY-MM-DD" }   # optional, must not touch test
  test:       { start: "YYYY-MM-DD", end: "YYYY-MM-DD" }
  split_type: walk_forward | rolling_window | anchored
  purge_embargo_hours: 0      # gap between splits to prevent overlap leakage

universe:
  assets: [EXPLICIT LIST OR A DOCUMENTED SELECTION RULE]
  inclusion_rule: "..."       # how an asset enters the sample
  survivorship_policy: delisted_retained | delisted_excluded | point_in_time_index
  quote_currency: USD
  notes: "..."

execution:
  signal_time:   "bar close of bar k, exchange timestamp"
  execution_time:"open of bar k+1, exchange timestamp"
  price:         mid | bid | ask | vwap | next_open
  latency_ms:    0             # 0 only if the above ordering is actually guaranteed
  timezone: UTC

costs:
  commission_bps:   0.0
  spread_model:     quoted | half_spread | fixed_bps
  spread_bps:       null      # required unless quoted
  slippage_model:   none | fixed_bps | volume_impact
  slippage_bps:     null      # required unless none/volume_impact is justified
  funding:          applied | ignored            # perpetual futures MUST be applied
  funding_interval_hours: 8

data:
  provider:        "..."
  dataset_version: "..."      # immutable snapshot id, not "latest"
  timezone:        UTC
  timestamped_at:  "YYYY-MM-DDTHH:MM:SSZ"   # when this was pinned to a snapshot
```

Mapping onto the metadata block above:

| Block | Metadata field it pins | What a missing value actually means |
|---|---|---|
| `temporal_protocol` | `time_period` | The evaluation is uninterpretable: with an arbitrary split, every result is available by choosing dates. |
| `universe` | `universe` | The return may come entirely from assets that were dead or delisted. The instrument list is a hidden degree of freedom. |
| `execution` | `execution_assumptions` | Firing at the close of the same bar that produced the signal is look-ahead by construction. |
| `costs` | `cost_model` | Without spread and slippage, gross and net are the same number and the claim is unfalsifiable. |
| `data` | `data_version`, `feature_version` | The result cannot be re-derived later. "Latest" is not a dataset. |

The three worst-covered blocks in the published literature — costs, universe,
splits — are exactly the three that require the author to commit to a choice
they would rather leave open. **An omitted protocol field is a free parameter,
and free parameters are where published claims go to die.**

Cost blocks are mandatory, not optional, for perpetual futures. A hypothesis
about liquidations or funding on a perpetual venue without `costs.funding:
applied` is incomplete by rule.

---

## The multidimensional evaluation contract

The Moon Dev AI Trading Battles benchmark deliberately **does not rank on raw
dollars**. It weighs risk-adjusted performance and drawdown. Raw profit is the
most easily gamed and least informative axis available: it rewards position
size, leverage and a single lucky regime, none of which are properties of an
idea. Drawdown and risk-adjusted measures are what remain when those are
controlled away.

> **We will NEVER store a single scalar called `strategy_score`.**

A scalar is a rank-ordering device, and rank-ordering devices are exactly where
an irreducible amount of judgement gets laundered into apparent objectivity.
Worse, a scalar compounded across research-quality dimensions reproduces the
flaw already rejected elsewhere in this architecture: give the generator one
number to optimize and it will optimize the number. The dimensions stay
separate and individually inspectable.

### The result object

```yaml
result:
  research_id: R_a8748a9efc10027c
  hypothesis_id: H0056
  rule_version: "188"
  data_version: dv_20261006_9f3c1a

  # returns
  gross_return: ...
  net_return: ...          # after the cost model. Gross alone is not a result.

  # risk
  volatility: ...
  max_drawdown: ...
  sharpe: ...

  # behaviour
  turnover: ...
  hit_rate: ...

  # integrity -- these are not performance, they are trustworthiness
  stability: ...           # does it survive re-fitting / sub-period splitting
  regime_dependence: ...   # does it only work in one regime
  exposure: {market: ..., style: ..., volatility: ...}

  # attribution
  residual_alpha: ...      # return left after market/factor/regime exposure
```

### Why each grouping exists

**Returns — `gross_return` and `net_return`, stored as two numbers, never one.**
Gross alone is not a result. The entire difference between the two is the cost
model, and the cost model is the assumption, not the measurement. Storing only
the post-cost number hides whether an idea works before frictions or only
because someone was optimistic about the spread. Storing only the pre-cost
number is a category error: no one trades at mid.

**Risk — `volatility`, `max_drawdown`, `sharpe`.** This is the axis the benchmark
deliberately chose. A return without a drawdown is a number that says nothing
about whether anyone could have held it. `max_drawdown` in particular is what
distinguishes a mechanism from a single lucky regime: the mechanism repeats,
the regime does not. `sharpe` sits here rather than in returns because it is a
function of both.

**Behaviour — `turnover`, `hit_rate`.** These describe *how* the money was made,
and they are the early warning on cost sensitivity. A high-`turnover`,
low-`hit_rate` result is one where the cost model dominates the outcome — which
means the result is a statement about the fee schedule, not about the
hypothesis. Without these two numbers a reader cannot tell that.

**Integrity — `stability`, `regime_dependence`, `exposure`.** These are not
performance and must not be read as performance. `stability` asks whether the
result survives re-fitting and sub-period splitting; `regime_dependence` asks
whether it works at all outside the one regime it was found in; `exposure` is
the measured market, style and volatility-factor bet the strategy is actually
making. They are grouped together because they are the trustworthiness
question — "should this number be believed?" — and answering it is what
separates a finding from a curve fit.

**Attribution — `residual_alpha`.** This is the KTD-Fin lesson, and it is the
single most important field in the object.

### Positive return is not alpha

A realised positive return does not identify where it came from. The same
number is consistent with the proposed mechanism, with market beta, with a
style or factor exposure, with a favourable regime, and with a universe
definition. A long-only strategy in a bull market has a positive return whether
or not the proposed mechanism exists at all.

Attribution is what distinguishes these, and attribution is only possible
against a **claimed mechanism**. You cannot ask "is this result consistent with
the mechanism we proposed?" if nobody recorded the mechanism at proposal time.

This is why `expected_mechanism` is a required, non-optional field on the queue
row, and why it is written as a causal path — actor, action, consequence — and
not as a restatement of the prediction. "Funding predicts reversal" is not a
mechanism. "Levered longs pay funding, forced unwinding sells, and that selling
is the reversal" is. A restatement gives Layer 2 nothing to test the result
against, and the attribution key becomes unusable.

So: Layer 1 records the claimed mechanism precisely at proposal time so that
Layer 2, months later, can ask whether the observed return is consistent with
it. If `net_return` is healthy and `residual_alpha` is approximately zero, the
return came from the exposure, not from the mechanism — and the hypothesis has
been refuted as an *alpha* claim despite the profit. If `residual_alpha` is
healthy but the exposure block is extreme, the mechanism is doing the work and
the sizing is doing the risk. Both readings are actionable; a scalar would have
hidden the difference between them.

### Why these are not a target

**This section describes Layer 2's OUTPUT FORMAT. It does not give Layer 1
anything to optimize toward, and adding one would reopen a door that was
deliberately closed.**

The distinction is categorical:

- A **target** is a number a component is rewarded for approaching. Layer 1 has
  none, and cannot be given one: `RuleSpec` sets `extra="forbid"`, a recursive
  SQL CHECK rejects any profitability key from a formalized spec, and the
  formalizer fails closed on the same scan. Moon Dev's own implementation uses
  `TARGET_RETURN = 50` and `SAVE_IF_OVER_RETURN = 1%`; this repository rejects
  both, because a research layer that can see a profitability target becomes a
  backtest-gaming machine.
- An **output format** is the shape of what Layer 2 hands back, written down so
  that the report cannot quietly collapse into one number. Layer 1 never reads
  these fields, never stores them, and never scores a hypothesis by them.

Layer 1's own scoring is on *research quality* — mechanism clarity,
falsifiability, evidence quality, novelty, data availability, temporal
integrity, implementation clarity, confounding risk — eight dimensions, each
0–5, all sharing the direction "higher is better", summed only into a
`total_score` and a coarse `priority_band` (reject / weak / experiment /
high_priority). Bands, not a continuous rank: a rank is a comparison between
ideas, a band is a statement about one. That is a different object from a
performance metric — it is measured before any data exists, and a hypothesis can
score 40/40 there and still lose money.

If you find yourself wanting to add `min_sharpe` or `target_net_return` to the
queue row or the spec, that is the bug, not the feature.

---

## The reproducibility requirement

A survey of **77 LLM trading-agent studies** — the **Agentic Trading survey**
— found that only **19** met primary empirical criteria. Of those 19:

- only **2 of 19** reported extractable, time-consistent split protocols;
- only **1 of 19** reported an explicit transaction-cost model;
- only **1 of 19** documented universe / survivorship handling;
- **11 of 19** reported execution timing / semantics;
- **none** reached the survey's highest reproducibility level.

Read those numbers as a funnel. Roughly one study in ten could even be split in
time consistently; roughly one in twenty could be re-run with costs. The
overwhelming majority of published claims in agentic trading are **not falsifiable
by a third party**, because the reader is not told how the clock was set, what
the spread was, or which instruments were alive.

**What these figures are and are not.** They measure *reporting discipline*, not
whether the underlying strategies worked. A study with no cost model does not
have bad returns; it has **unmeasurable** returns. They are an argument for
protocol rigour and nothing else. They are never a performance benchmark, and no
target in this repository is derived from them.

The direct consequence for Layer 2: **a Layer 2 result is only meaningful if
the result object carries the full controlled-experiment metadata block.** A
result without a pinned `data_version` is not reproducible. A result without a
`cost_model` cannot be distinguished from a result computed with optimistic
assumptions. A result without `universe` and `survivorship_policy` cannot be
joined back to the instruments that produced it. There is no partial credit —
the metadata is complete or the experiment does not exist.

---

## The minimum Layer 2 must do

1. **Read a queued job.** Filter `status = 'queued'`.

2. **Claim it before doing any work.**

```sql
UPDATE experiment_queue SET status = 'claimed', claimed_at = now()
 WHERE experiment_id = $1 AND status = 'queued';
```

Check that one row changed. Zero rows means another worker won the race.
3. **Pin the dataset.** Resolve `pending_l2_dataset_selection` to a concrete,
   immutable snapshot identifier and carry it in the **result record**, not on
   the queue row — the trigger forbids the latter. See
   [`data_version` is frozen](#data_version-is-frozen-the-pin-lives-in-the-result).

4. **Compute `feature_set` as written.** These are the concrete inputs. If Layer 2
   cannot compute one of them from available data, that is a **data availability
   failure, not a failed hypothesis** — record it as such and do not silently
   substitute a proxy. A proxy turns a clean refutation into an unfalsifiable
   muddle, and the corpus then records a claim about a variable nobody measured.
   Record the concrete feature list under `feature_version`.

5. **Evaluate `rule_spec.hypothesis.signal.condition` as written.** It is an
   expression, not English. If it cannot be evaluated unambiguously, the
   formalizer failed — report it through the critique path
   (`POST /hypotheses/{id}/critique`, or `failure_type = unformalizable`) rather
   than improvising an interpretation. Two people implementing the same spec must
   produce the same trades; that is what `hidden_discretion` as a critique
   dimension is protecting.

6. **Test against the stated null.** `rule_spec.hypothesis.null` names the
   observation that would refute the claim. A backtest that finds "no signal"
   has refuted it. One that finds a small effect has also refuted it as an
   *economic* claim once costs are included — the honest report records both the
   gross effect and the net effect, so the reader can see which killed it.

   Not every spec carries a return. A formalized rule may assert a *relationship*
   (a correlation, a sign agreement) with no directional expression, in which
   case there is nothing to trade and no P&L is defined. The correct output for
   such a row is the measurement of the stated null, with the return fields
   explicitly undefined — not an invented directional rule, and not a zero.

7. **Compute the multidimensional result.** Every grouping in the result object,
   including the integrity and attribution block. `net_return` is the headline;
   `residual_alpha` is the one that decides whether the mechanism earned it.

8. **Complete the job.**

```sql
UPDATE experiment_queue SET status = 'completed', completed_at = now()
 WHERE experiment_id = $1;
```

9. **Report a negative result.** See the next section. A refuted hypothesis is
   knowledge.

---

## Writing results back: `failures`

Layer 2's write surface into Layer 1's tables is exactly one table, `failures`,
and it writes there only to record that an experiment did not support its
hypothesis.

```
POST /failures  (SQL equivalent; the HTTP route is read-only today)
```

```sql
INSERT INTO failures (hypothesis_id, reason, failure_type, stage, reported_by)
VALUES ($1, $2, $3, 'layer2_backtest', 'layer2.backtest_engine')
RETURNING failure_id;
```

`stage` is `CHECK`-constrained to `('critic', 'layer2_backtest')` and is what
distinguishes a hypothesis the **critic** killed from one the **data** killed.
That distinction is load-bearing: a critic failure means the claim was never
worth testing, while a `layer2_backtest` failure means the claim was well-formed,
passed every gate, consumed a backtest, and did not survive. The second is a
much stronger statement of knowledge than the first.

`failure_type` is validated against the `failure_type` enum, so an unknown value
is rejected by name rather than surfacing as a raw cast error.

### A negative result is knowledge

A refuted hypothesis is a successful experiment. The pipeline got an idea, wrote
it down precisely enough to be testable, tested it under controlled conditions,
and learned the answer. That is the entire job. A run that only ever reports
positive results has learned nothing about which mechanisms are false, and the
corpus cannot tell a working discovery process from a lucky one.

### The loop closes here

The Research Critic reads `failures` before critiquing anything new — and it
reads them at **every stage**, not just its own. `critic._prior_failure_digest`
queries every `failures` row joined to its mechanism, orders same-mechanism
collisions to the top of the context, and hands the digest to the model as
`prior_failures` so `no_prior_failure_collision` is scored against what is
actually known to be false rather than against the model's recollection. It
also reports the split:

```sql
SELECT count(*) AS total,
       count(*) FILTER (WHERE stage = 'critic')         AS from_critic,
       count(*) FILTER (WHERE stage = 'layer2_backtest') AS from_data
  FROM failures;
```

So a Layer 2 failure makes the next generation of related hypotheses **stricter
than the first**. The generator proposes a forced-flows mechanism; the critic
passes it; Layer 2 refutes it on cost grounds and records
`stage='layer2_backtest'` with the specific defect in `reason`; the next
generator proposes a near-sibling; the critic sees the recorded refutation and
refuses it as `prior_failure_precedent` rather than letting a dead branch
consume another backtest.

That is the whole reason `failures` is a table and not a log line. Without the
read-back, each generation restarts from the same ignorance and the corpus
accumulates strings rather than knowledge.

---

## The failure database

```
GET /failures?failure_type=not_falsifiable&limit=100
```

| `failure_type` | Usually written by | Meaning |
|---|---|---|
| `implausible_mechanism` | critic | No coherent economic story. |
| `not_measurable` | critic **or Layer 2** | Features cannot be computed from real data — including from the pinned dataset. |
| `not_falsifiable` | critic | No concrete observation would refute it. |
| `not_distinct` | critic | A reworded sibling of an existing claim. |
| `hidden_discretion` | critic | Discretionary judgement smuggled into the signal. |
| `future_information_leak` | critic | Uses data unavailable at decision time. |
| `prior_failure_precedent` | critic | Repeats something already refuted. |
| `unformalizable` | critic, or Layer 2 on a spec it cannot evaluate | Cannot be expressed as a computable rule. |
| `malformed_llm_output` | critic | Model output unusable. |

A Layer 2 result of "no edge" is normally `not_falsifiable` (the claim died as
stated) or `not_measurable` (the data could not speak to it at all). Choose the
type that best describes **why** — the table is only useful if the reason is
specific. "Didn't work" is not a reason, and `record_failure` rejects an empty
one. Name the observation, the window, and the condition that failed:

```
multi_level_ofi and aggressive_volume showed no significant co-movement at the
1.5-sigma threshold in 2023-01-01..2024-12-31 (n_triggered=412, mean_forward_1d
return = 3bp, gross) and the trigger count falls below 30 once the stated cost
model is applied, so the stated null was not refuted but the edge is not
economically material.
```

---

## Worked example: one row, end to end

A row exactly as `GET /experiments/queue?status=queued` returned it during a real
pipeline run (quoted verbatim; the same shape recurs on any queue read):

```json
{
  "experiment_id": "EXP003",
  "research_id": "R_a8748a9efc10027c",
  "status": "queued",
  "queued_at": "2026-10-06T17:43:32Z",
  "feature_set": ["multi_level_ofi", "aggressive_volume"],
  "rule_version": "188",
  "data_version": "pending_l2_dataset_selection",
  "parameter_space": {},
  "version_id": 188,
  "hypothesis_id": "H0056",
  "hypothesis_statement": "The multi-level order flow imbalance (multi_level_ofi) will show a significant positive correlation with the aggressive volume (aggressive_volume) during periods of market stress, indicating that more sell intent is being expressed through market orders.",
  "expected_mechanism": "During periods of market stress, passive limit sellers may not be able to replenish the order flow imbalance quickly enough, leading to an imbalance that is more pronounced and persistent.",
  "rule_spec": {
    "name": "order_flow_imbalance_stress",
    "market": { "universe": "global_stock_market", "asset_class": "equities" },
    "features": ["multi_level_ofi", "aggressive_volume"],
    "hypothesis": {
      "statement": "…same as above…",
      "null": "multi_level_ofi and aggressive_volume are not significantly correlated during periods of market stress",
      "signal": {
        "condition": "(zscore(multi_level_ofi) > 1.5 and zscore(aggressive_volume) > 1.5) or (zscore(multi_level_ofi) < -1.5 and zscore(aggressive_volume) < -1.5)",
        "horizon": "1 day",
        "notes": "Significant positive correlation between multi-level order flow imbalance and aggressive volume during periods of market stress"
      }
    },
    "expected_mechanism": "During periods of market stress, passive limit sellers may not be able to replenish the order flow imbalance quickly enough, leading to an imbalance that is more pronounced and persistent."
  },
  "source_ids": ["SRC005"],
  "model_used": "ollama/qwen2.5:3b-instruct",
  "prompt_version": "l1-v1",
  "creation_timestamp": "2026-10-06T17:43:31Z"
}
```

### The Layer 2 work this implies, step by step

**Step 1 — Claim.** `UPDATE experiment_queue SET status='claimed', claimed_at=now() WHERE experiment_id='EXP003' AND status='queued'`. Verify `rowcount == 1`.

**Step 2 — Pin the dataset.** `data_version` reads `pending_l2_dataset_selection`.
Layer 2 owns this choice. Resolve the universe implied by
`market.universe = "global_stock_market"` against an actual snapshot and produce
`data_version: dv_20261006_9f3c1a`. It goes in the result record; the queue row
is untouched and will keep reading the sentinel forever, which is correct.

**Step 3 — Read the feature names against what the dataset can actually
produce.** `multi_level_ofi` and `aggressive_volume` must be computable
point-in-time from L2 data. If the snapshot only carries top-of-book depth and
no trade-level aggressor flags, `aggressive_volume` is unavailable. That is a
`not_measurable` failure at `stage='layer2_backtest'` — **not** a refuted
hypothesis, and **not** grounds for substituting signed volume as a proxy. A
proxy would produce a number attributed to a claim about a variable nobody
measured.

**Step 4 — Freeze the feature version.** Order the features, pin the z-score
lookback, hash it: `feature_version: fv_8c1d`. The `zscore()` in the condition
has no stated lookback, which is exactly the kind of hidden degree of freedom
the metadata block exists to close.

**Step 5 — Pin the conditions.** Fill in the block: `universe`,
`time_period`, `execution_assumptions` (fill model `next_bar_open`, latency 0
only if the bar-close → next-open ordering is actually guaranteed by the
dataset), `cost_model`, `random_seed`.

**Step 6 — Evaluate the condition as written.** The condition is a symmetric
co-movement test: it fires when both z-scores exceed ±1.5 in the *same*
direction. Two people reading this must produce identical trigger sets. Note
what it is *not*: there is no direction, no side, and no position rule. This
spec asserts a **relationship**, not a trade.

**Step 7 — Test the stated null.** The null is
*"multi_level_ofi and aggressive_volume are not significantly correlated during
periods of market stress"*. So the primary measurement is the correlation
between the two series on triggered bars, at the stated `1 day` horizon. There
is no P&L to compute, because the spec never states a side — and inventing one
would be the single most damaging thing Layer 2 could do here. The result
object's return, risk and behaviour fields are explicitly undefined for this
row, and that is a correct result, not an incomplete one. A directional
specification is what a future generation should propose; Layer 2 does not get
to supply it.

**Step 8 — Fill the result object.** (The numbers below are illustrative — no
backtest has been run against this row. The *shape* is what matters.)

```yaml
result:
  research_id: R_a8748a9efc10027c
  hypothesis_id: H0056
  rule_version: "188"
  data_version: dv_20261006_9f3c1a

  gross_return: undefined   # spec states a correlation, not a direction
  net_return: undefined
  volatility: undefined
  max_drawdown: undefined
  sharpe: undefined
  turnover: undefined
  hit_rate: undefined

  integrity:
    stability: high          # relationship is stable across sub-periods
    regime_dependence: high  # …and only holds in stress, which is what was claimed
    exposure: {market: low, style: not_measured, volatility: not_measured}
  correlation:
    pearson_r: 0.11          # measured on triggered bars
    n_triggered: 412
    n_bars: 187_440
    window: 2023-01-01..2024-12-31
    significance: see report
  attribution:
    residual_alpha: undefined  # no return, so nothing to attribute
    mechanism_consistency: "observed co-movement is consistent with the stated
      mechanism: passive replenishment lag in stress leaves imbalance and
      aggressive volume moving together"
```

**Step 9 — Complete or fail.**

```sql
UPDATE experiment_queue SET status='completed', completed_at=now()
 WHERE experiment_id='EXP003';
```

If instead the correlation had come back indistinguishable from zero, the null
stands and the row is a negative result:

```sql
INSERT INTO failures (hypothesis_id, reason, failure_type, stage, reported_by)
VALUES ('H0056',
        'multi_level_ofi and aggressive_volume showed no significant co-movement
         at the 1.5-sigma threshold over 2023-01-01..2024-12-31 (n_triggered=412,
         pearson_r=0.03); the stated null was not refuted',
        'not_falsifiable', 'layer2_backtest', 'layer2.backtest_engine')
RETURNING failure_id;
```

Either way `expected_mechanism` was on the row, so step 8's
`mechanism_consistency` field could be written at all. That is the entire reason
Layer 1 insists the mechanism be a causal path rather than a restatement of the
prediction: an attribution question with no mechanism to test against cannot be
answered, however good the backtest is.

---

## What Layer 1 deliberately does not give you

- **No expected return.** There is no `expected_return` field anywhere in Layer 1,
  and `RuleSpec` sets `extra="forbid"` so one cannot be smuggled in through the
  API. A profitability target is an optimization objective; letting the research
  layer hold one makes it optimize toward whatever makes a backtest look good.
  Layer 2 decides whether a mechanism has evidence.
- **No strategy score.** No scalar, no ranking of hypotheses, no "8.5 out of 9,
  therefore queue". Quality dimensions are recorded individually and a single
  FAIL blocks; averaging them would let a strong mechanism buy a look-ahead.
- **No signal, no position sizing, no execution.** `rule_spec` states a
  measurable relationship and the conditions under which it should hold. What to
  trade, at what size, with what stop, is Layer 2's problem, informed by Layer 2's
  risk model.
- **No data access.** Layer 1 never fetches a price. It has no idea which
  datasets exist, which is why `data_version` arrives as a sentinel and why the
  universal is Layer 2's to state.
- **No cost model, no default cost model.** A default is a free parameter in
  disguise: it lets the author skip a choice while still consuming it.
- **No results table.** Layer 1 does not store performance. The only table Layer 2
  writes into is `failures`, and only to record a refutation.

## Provenance contract

Every queued row carries the full tuple required to reproduce it later:

```
research_id      hypothesis_id    source_ids    feature_set
rule_version     data_version     parameter_space
creation_timestamp   model_used      prompt_version
```

Two runs of the same hypothesis against the same rule and data version must
produce the same result. When they do not, one of these fields differs — which
is exactly why they are all stored. Combined with the Layer 2 metadata block,
this is the difference between an experiment and an anecdote.

## Epistemic status

The queue columns, the immutability trigger, the `failures` constraints and the
`EXP003` worked example were read from `db/schema.sql`,
`db/schema_evidence_ledger.sql`, `layer1/contracts.py`,
`layer1/components/queueing.py` and the live database.

The controlled-comparison principle and the multidimensional evaluation axes
come from [knowledge/moondev/controlled_experiment_design.md](../knowledge/moondev/controlled_experiment_design.md),
read from Moon Dev's AI Trading Battles repository. The attribution requirement
comes from [knowledge/research_integrity/knowledge_leakage.md](../knowledge/research_integrity/knowledge_leakage.md)
(KTD-Fin). Neither is evidence that any agent, model or strategy performed well
in any such setting; both are evidence about **method**.

The Agentic Trading survey figures — 77 studies, 19 primary, 2 of 19 with
extractable time-consistent splits, 1 of 19 with an explicit transaction-cost
model, 1 of 19 documenting universe/survivorship handling, 11 of 19 reporting
execution timing/semantics, none reaching the highest reproducibility level —
are **user-supplied** and have not been verified against the survey text here.
Use them as motivation for protocol rigour, never as measurements of any
strategy. If the survey is later read directly, correct the figures in
[knowledge/research_integrity/reproducibility_audit.md](../knowledge/research_integrity/reproducibility_audit.md)
first, and update them here.

## Related

- [examples/evaluate_multidimensional.md](../examples/evaluate_multidimensional.md) —
  the result object as a hand-off page, with a filled-in example and the two
  mistakes that most often invalidate a Layer 2 result
- [../knowledge/moondev/controlled_experiment_design.md](../knowledge/moondev/controlled_experiment_design.md) —
  where the metadata contract and the no-scalar rule come from
- [../knowledge/research_integrity/reproducibility_audit.md](../knowledge/research_integrity/reproducibility_audit.md) —
  the five protocol blocks and the survey funnel
- [../knowledge/research_integrity/knowledge_leakage.md](../knowledge/research_integrity/knowledge_leakage.md) —
  why attribution replaces raw returns