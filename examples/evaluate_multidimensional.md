# Evaluating a queued experiment: the result object

Companion to **[docs/LAYER2.md](../docs/LAYER2.md)**. That document is the
contract; this is the page to hand whoever builds Layer 2. It gives the result
object, a filled-in example, and the two mistakes that most often invalidate a
Layer 2 result.

---

## The shape

```yaml
result:
  research_id: ...            # from the queue row
  hypothesis_id: ...          # from the queue row
  rule_version: ...           # from the queue row
  data_version: ...           # PINNED BY LAYER 2 -- replaces the sentinel

  # returns
  gross_return: ...
  net_return: ...             # after the cost model. Gross alone is not a result.

  # risk
  volatility: ...
  max_drawdown: ...
  sharpe: ...

  # behaviour
  turnover: ...
  hit_rate: ...

  # integrity -- not performance, trustworthiness
  stability: ...             # survives re-fitting / sub-period splitting?
  regime_dependence: ...     # works outside the one regime it was found in?
  exposure: {market: ..., style: ..., volatility: ...}

  # attribution
  residual_alpha: ...        # return left after market/factor/regime exposure
```

**Never collapse these into one number.** There is no `strategy_score`, and
adding one defeats the purpose: a scalar is a rank-ordering device, and
rank-ordering devices launder judgement into apparent objectivity. Each grouping
answers a different question — did it make money, was it survivable, how did it
behave, should the number be believed, and did the *claimed mechanism* earn the
return. Collapsing them destroys the answer to the last question, which is the
only one that connects the result back to the hypothesis Layer 1 proposed.

The object travels with the full controlled-experiment metadata block
(`universe`, `time_period`, `feature_version`, `execution_assumptions`,
`cost_model`, `random_seed`). A result without a pinned `data_version` is not
reproducible — see
[the reproducibility requirement](../docs/LAYER2.md#the-reproducibility-requirement).

> `data_version` on the **queue row** is frozen by trigger and will keep reading
> `pending_l2_dataset_selection` forever. The pin you resolve goes in the
> **result record**, keyed by `experiment_id` / `research_id`.

---

## A filled-in example

A directional job, `EXP009`, claiming: *funding paid by levered longs precedes
reversal in the following bar.* The figures are illustrative — no backtest has
been run. What to copy is the **shape**: which fields exist, which are grouped,
and which are deliberately absent.

```yaml
result:
  research_id: R_3f9c2ad41b7e0155
  hypothesis_id: H0061
  experiment_id: EXP009
  rule_version: "193"
  data_version: dv_20261006_9f3c1a

  # --- controlled-experiment conditions (Layer 2 supplies these) ---
  universe: crypto_perp:BTCUSDT,ETHUSDT,SOLUSDT   # explicit list, not "top 3 by volume"
  survivorship_policy: delisted_retained
  time_period: 2023-01-01..2024-12-31
  temporal_protocol:
    train:      { start: "2023-01-01", end: "2023-12-31" }
    validation: { start: "2024-01-01", end: "2024-06-30" }
    test:       { start: "2024-07-01", end: "2024-12-31" }
    split_type: walk_forward
    purge_embargo_hours: 24
  feature_version: fv_8c1d
  execution_assumptions:
    order_type: market
    fill_model: next_bar_open
    decision_latency_ms: 0
    participation_cap: 0.05
  cost_model:
    fee_bps: 4.0
    half_spread_bps: 1.5
    slippage_model: fixed_bps
    slippage_bps: 2.0
    funding: applied
    funding_interval_hours: 8
  random_seed: 1729

  # --- returns ---
  gross_return: 0.412          # 41.2% over the test window
  net_return:   0.063          # 6.3% -- the cost model took 35 points

  # --- risk ---
  volatility:   0.71           # annualized
  max_drawdown: -0.29
  sharpe:       0.41

  # --- behaviour ---
  turnover:     318.4          # annual notional turnover, x
  hit_rate:     0.512          # fraction of positions that closed positive

  # --- integrity: should this number be believed? ---
  stability:         high      # holds in 4/5 walk-forward folds; refit params shift <15%
  regime_dependence: low       # works in high- and low-funding regimes alike
  exposure:
    market:    -0.02           # near-zero beta; not a disguised long-btc book
    style:      0.11           # mild carry/size tilt
    volatility: 0.34           # substantial low-vol carry tilt

  # --- attribution: did the claimed mechanism earn it? ---
  residual_alpha: 0.024        # 2.4% after removing market/style/vol exposure
  mechanism_consistency: consistent
  mechanism_note: >-
    The residual concentrates on bars where funding was positive and open
    interest was falling into the signal -- i.e. longs paying to be in positions
    they were unwinding, which is the stated mechanism rather than a generic
    funding-level effect.

  # --- the stated null, tested ---
  null_tested: >-
    "funding level at bar k does not predict sign-reversal of the next-bar
    return, conditional on the signal firing"
  null_result: rejected_on_test_window_only
  null_note: >-
    Permutation test, 1000 draws, seed 1729: the reversal rate on triggered bars
    is 0.512 against a 0.500 base rate, p = 0.031. The null is rejected on the
    TEST window only; train and validation folds are within noise.
```

Read it top to bottom and the shape tells the story: 41% gross collapsing to 6.3%
net says the cost model did most of the work; `turnover: 318` explains why;
`residual_alpha: 0.024` says the mechanism is real but small; and the null note
says the effect is only visible in the held-out window, which is exactly the
finding a single-number report would have hidden.

### When a spec states a relationship, not a trade

Some formalized rules assert a correlation or a sign agreement with no directional
expression — a real queued row, `EXP003`, asserts that `multi_level_ofi`
co-moves with `aggressive_volume` during stress, and its condition is a
symmetric two-sided z-score test with no side and no position rule. There is
nothing to trade, so no P&L is defined. The correct output is:

```yaml
  gross_return: undefined
  net_return: undefined
  # ... risk and behaviour likewise undefined ...
  correlation:
    pearson_r: 0.11
    n_triggered: 412
    n_bars: 187_440
  mechanism_consistency: "consistent with the stated replenishment-lag mechanism"
```

Explicitly `undefined`, not zero and not invented. Supplying a direction the spec
does not state means testing something other than what was queued.

---

## The two mistakes that most often invalidate a Layer 2 result

### 1. Reporting gross instead of net

`gross_return` without `net_return` is not a smaller omission than it looks. The
gap between the two *is* the cost model, and the cost model is the assumption,
not the measurement. A strategy that turns over 318x a year with a 2bp
half-spread is a strategy whose result is a statement about the fee schedule.
Reporting the gross number alone removes exactly the evidence that would let a
reader see that.

Related discipline: `funding: applied` is mandatory for perpetual futures. A
forced-flows or funding hypothesis evaluated without funding costs is not a
conservative estimate — it is a different experiment.

Store both, always, and let the difference be the finding.

### 2. Forgetting to test the stated null

`rule_spec.hypothesis.null` names the observation that would refute the claim.
Every experiment has to answer it explicitly, because a backtest that reports
only a return has not reported whether the claim survived.

Three ways this goes wrong in practice:

- **Not testing it at all.** You compute a return, see it is positive, and stop.
  Positive is not sufficient; the null has to be *rejected*, with the evidence
  that rejected it.
- **Testing the wrong null.** Substituting "no signal" for the stated one. The
  stated null is often finer than that — "not significantly correlated *during
  periods of market stress*" is a conditional claim and needs a conditional
  test. Testing an unconditional version of it can reject a null that actually
  stands.
- **Treating a small effect as a survival.** An effect that is real and an effect
  that is *economically* material are different findings. A 3bp edge at 318x
  turnover is an artifact of the fee assumption. When costs kill it, that is a
  refutation of the claim as an *economic* claim, and it belongs in `failures`
  at `stage='layer2_backtest'` with the numbers in the reason.

Report the null result explicitly — `null_tested`, `null_result`, `null_note` —
so a later reader can see the refutation attempt rather than inferring from
silence.

Use an explicit enum, not a yes/no: `rejected`, `not_rejected`,
`rejected_on_test_window_only`, `inconclusive_insufficient_power`. A bare
`not_observed` is ambiguous in exactly the place it matters — it can be read as
"the null was not observed" or "the effect was not observed", which are opposite
findings.

---

## Reporting a refutation

A negative result goes back into Layer 1's only write surface:

```sql
INSERT INTO failures (hypothesis_id, reason, failure_type, stage, reported_by)
VALUES ($1, $2, $3, 'layer2_backtest', 'layer2.backtest_engine')
RETURNING failure_id;
```

Then close the job:

```sql
UPDATE experiment_queue SET status='completed', completed_at=now()
 WHERE experiment_id = $1;
```

The Research Critic reads `failures` before critiquing anything new, which is
what makes the second generation of hypotheses stricter than the first. See
[writing results back](../docs/LAYER2.md#writing-results-back-failures).

---

## Reminder: this is an output format, not a target

Nothing above is something Layer 1 optimizes toward. These are the fields Layer 2
*emits*, written down so the report cannot quietly collapse into one number.
Layer 1 never reads them, never stores them, and never scores a hypothesis by
them; its own scoring is on research quality — mechanism clarity,
falsifiability, evidence quality, novelty, data availability, temporal integrity,
implementation clarity, confounding risk — each 0–5, all sharing the direction
"higher is better", summed only into a `total_score` and a coarse
`priority_band`. That is a different object from a performance metric: it is
measured before any data exists, and a hypothesis can score 40/40 there and
still lose money.

If you are tempted to add `min_sharpe` or `target_net_return` to the spec or the
queue row, that is the bug. Full reasoning in
[why these are not a target](../docs/LAYER2.md#why-these-are-not-a-target).