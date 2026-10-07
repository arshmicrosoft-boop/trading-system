# Contradiction Resolution

What to do when two sources disagree. The short version: **never average.** A contradiction between sources is the most valuable raw material the research engine has.

Related: [REPLICATION.md](REPLICATION.md) (the safest thing to do with a contested claim) · [hypothesis_testing.md](hypothesis_testing.md) (narrowing a hedge is the failure mode's cousin) · [failure_modes.md](failure_modes.md) · [HYPOTHESIS_SCHEMA.md](HYPOTHESIS_SCHEMA.md) · [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md) (`CONTRADICTION` is a first-class mutation type) · [../frameworks/methodology_references.md](../frameworks/methodology_references.md)

## The pattern

Paper A: order flow imbalance predicts short-horizon returns. Paper B: order flow imbalance has no exploitable edge once transaction costs are paid.

Both can be correct. They are usually making claims about different objects — different cost assumptions, different horizons, different definitions of "order flow imbalance" (contemporaneous vs. lagged, aggregated vs. top-of-book, computed over trade sign vs. book side), different universes. The engine's job is to find the condition under which both hold, not to pick a winner.

This is not a rare edge case. It is the normal state of a research corpus assembled from multiple tiers — peer-reviewed work, practitioner writing, blog posts, video — where sampling frequency and cost models differ between almost every pair of sources.

## The forbidden move: averaging

The tempting resolution is to split the difference: "OFI has *some* predictive power, though costs reduce it." **Do not do this.**

An averaged claim has the property that no possible result refutes it. If a backtest shows a strong gross edge, the averaged claim is satisfied; if it shows the edge disappears entirely under costs, the averaged claim is *also* satisfied, because "costs reduce it" was conceded in advance. It has arranged to survive both outcomes, which means it constrains nothing — it is `not_falsifiable` in exactly the sense catalogued in [failure_modes.md §2](failure_modes.md).

It is worse than useless, because it is laundered. A hedged claim looks like a synthesis of two rigorous sources and is stored as such, but it carries none of either one's force. Downstream, a Layer 2 result will "confirm" it and the corpus will record agreement that was never tested.

Averaging also destroys the information the contradiction carried. The disagreement between A and B is *about* the cost model, or the horizon, or the definition — and that disagreement is the hypothesis. Averaging discards the answer along with the question.

## The correct move: record the conflict

Before generating anything, write down the disagreement as an explicit object:

```
conflict:
  claim_a:      <what source A asserts, with source_id and tier>
  claim_b:      <what source B asserts, with source_id and tier>
  possible_reasons:
    market:          different venue, asset class, or market structure
    timeframe:       different sampling frequency or prediction horizon
    cost_model:      one side modelled fees, spread, slippage; the other did not
    feature_definition: same name, different construction
    sampling_frequency: same horizon, different bar size or aggregation
    regime:          measured in one volatility or liquidity regime only
  status:         unresolved
```

`possible_reasons` is the important field and it must be **enumerated, not summarised**. The six reasons are a checklist, not prose. A conflict record that says "they used different assumptions" has recorded nothing; a conflict record that says "A reports gross returns at 5-minute aggregation on the 2023 majors; B reports net of taker fees at 1-minute aggregation on all crypto perps, and finds the sign flips" gives the next step.

Both `claim_a` and `claim_b` carry `source_ids`, because the resolution of a conflict is a claim about *those specific sources under those specific conditions*, and unattached claims cannot be re-examined when the corpus grows. Tier matters here: a tier-1 paper contradicting a tier-4 video is not a 50/50 disagreement — see the tier hierarchy in [../frameworks/methodology_references.md](../frameworks/methodology_references.md).

## The research move: convert the conflict into a hypothesis

This is the point. **A contradiction between sources is a research opportunity, not a defect to be smoothed over.** The disagreement names a condition, and a condition is what a hypothesis is for.

The conflict above yields:

> **Hypothesis (conditional).** Order flow imbalance predicts returns on the major venue at 5-minute aggregation, but does not survive a taker-fee cost model at 1-minute aggregation. The predictive power is conditional on the cost regime, and the boundary is where gross edge falls below transaction cost.
>
> - `mechanism`: liquidity provision is priced. The sign of the imbalance's value depends on whether the price impact it predicts exceeds the cost of taking it.
> - `prediction`: net predictive power decreases monotonically as the cost model becomes more realistic, and crosses zero at a definable cost level.
> - `direction`: conditional
> - `null`: net predictive power is invariant across cost models of increasing severity.
> - `alternative_explanations`: the difference is a horizon artefact rather than a cost artefact; or a sampling-frequency artefact unrelated to costs.
> - `expected_failure_conditions`: the relationship is unchanged under a realistic cost model, in which case source A's edge was not cost-borne and the conditional claim collapses into a contradiction that needs a different explanation.

Note the shape. It is not a compromise — it is a **stronger and more specific claim than either source made**, because it now predicts the direction of change under a stated manipulation of the cost model. Source A never said what would happen if costs rose. Source B never said what would happen if they fell. The new hypothesis does, and that prediction can lose.

Critically, this is also the `alternative_explanations` field doing its job. The cost explanation and the horizon explanation both fit the observed conflict; if the artifact does not separate them, the mechanism is a guess dressed as a finding.

### The generalisation

Every conflict in the corpus has the same structure, and the same move applies:

| conflict | hypothesis produced |
|---|---|
| A says X predicts; B says X is already priced in | X's predictive power is conditional on the degree to which it is public information |
| A finds the effect at 1m; B finds it at 1d | the effect's horizon scales with an unobservable liquidity parameter, not with the sampling rate |
| A reports it in crypto; B reports it not in equities | the mechanism depends on market structure specific to 24/7 fragmented venues |
| A's definition and B's definition disagree in sign | the two constructions are measuring opposite sides of the same queue |

Each is a conditional hypothesis with a stated direction, a stated null, and conditions — a well-formed artifact that is worth more than either source and is properly the output of a `CONTRADICTION` mutation.

## Where conflicts are recorded

Conflicts are **not** a new table. They are hypotheses with two or more `source_evidence` entries that disagree, plus `alternative_explanations` naming the reconciling conditions, plus `related_hypotheses` pointing at both ancestors. The storage already supports this: `research_identity.source_ids` is a `TEXT[]`, so a hypothesis derived from a conflict carries both sources natively, and the `experiment_queue_layer2` view hands Layer 2 the full provenance.

What must not happen is the conflict being *resolved at write time*. The corpus stores both claims and the reason they conflict. Resolution, if it comes, comes from Layer 2 running the conditional hypothesis — and then it comes with data, which is the only thing in this system entitled to settle an empirical disagreement.

## Epistemic status

This is our own operational method. The pattern (Paper A vs Paper B on order flow imbalance) is the archetypal case, not a documented episode from the literature; treat it as illustrative rather than as a citation. No claims in this file were verified against the user's research notes.

The one thing a future agent should not soften: **the prohibition on averaging is load-bearing.** Averaged claims do not merely read as cautious — they are structurally unfalsifiable, and they are the most natural way for a language model to resolve a contradiction it has been handed. Any change that lets a conflict be collapsed into a hedged claim at generation time reintroduces the failure this document exists to prevent.

A secondary caution: conflicts between sources of *different tiers* are not symmetric. When a tier-1 replication contradicts a tier-4 practitioner claim, that asymmetry should tilt the mechanism toward the practitioner's cost assumptions — but it should still be converted into a conditional hypothesis, not used to silently drop the practitioner claim from the corpus. Negative knowledge is first-class; see the negative-knowledge section of [failure_modes.md](failure_modes.md).