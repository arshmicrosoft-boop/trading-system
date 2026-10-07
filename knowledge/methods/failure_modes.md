# Failure Modes

This is the corpus's operational core. The Research Critic's checklist is assembled from this file, so every mode here must be checkable by a human reviewer or, where noted, by code.

**What this document is not.** It is not a list of things that went wrong in other people's research. It is the set of ways *our* pipeline produces artifacts that are worthless, plus the enum value each one maps to so the defect is recorded rather than forgotten.

Related: [HYPOTHESIS_SCHEMA.md](HYPOTHESIS_SCHEMA.md) (the object these modes are checked against) · [hypothesis_testing.md](hypothesis_testing.md) (the score these modes feed) · [REPLICATION.md](REPLICATION.md) (why replication avoids most of these) · [contradiction_resolution.md](contradiction_resolution.md) (source conflicts are not failures) · [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) (where this taxonomy becomes an operative checklist) · [../research_integrity/knowledge_leakage.md](../research_integrity/knowledge_leakage.md) · [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md) · [../frameworks/methodology_references.md](../frameworks/methodology_references.md) (tier hierarchy)

## The enum

Every mode maps to exactly one of the nine `failure_type` values, and these nine are the complete set — they are defined as a PostgreSQL `ENUM` in `db/schema.sql` and as a `Literal` in `layer1/contracts.py`. No mode below invents a tenth.

| value | one-line meaning |
|---|---|
| `implausible_mechanism` | the stated economic mechanism has no account of why it should exist |
| `not_measurable` | the variables named cannot be computed from data we have or could have |
| `not_falsifiable` | no observation could refute it, or it was fitted to noise |
| `not_distinct` | it restates something already in the corpus |
| `hidden_discretion` | an unnamed human/model decision determines the outcome |
| `future_information_leak` | the artifact uses information not available at decision time |
| `prior_failure_precedent` | we have already recorded this failing, under similar conditions |
| `unformalizable` | it cannot be turned into a deterministic `RuleSpec` |
| `malformed_llm_output` | the artifact is a bad generation, not a bad idea |

A `Critique` with `verdict: FAIL` must carry a `failure_type`; a `PASS` must not. The critic cannot express a vague rejection — see the `_coherent` validator in `layer1/contracts.py`.

The critic's *checklist* dimensions are seven, and they are not the same thing as the failure enum: `mechanism_plausible`, `measurable`, `falsifiable`, `distinct`, `no_hidden_discretion`, `no_future_leakage`, `no_prior_failure_collision` (each scored 0-2). A checklist dimension failing is what produces a verdict; the enum value is what gets recorded.

---

## 1. Temporal failures → `future_information_leak`

Look-ahead bias in any form is the same defect: the artifact reaches for information that did not exist at the moment it claims to act. It is grouped together because detection is the same job — ask, for every variable, *when is this value knowable, and by whom.*

**Look-ahead bias.** The signal uses a value computed over a window that extends past the decision timestamp. Manifests as: a smoothed series, a centred rolling mean, a Z-score normalised against the full-sample mean, or a forward return used as a feature. Programmatic detection: compute each feature's timestamp interval and require `feature_window_end <= decision_timestamp`. A centred window is a structural fail, not a threshold question.

**Future leakage.** The artifact depends on data that is revised, backfilled or published after the fact — a fundamentals figure restated in a later filing, a historical index membership list computed with hindsight, a `funding_rate` series finalised after the next funding event. Manifests as: an aggregate whose value at time *t* is only published at *t+k*. Detection: require an explicit `available_at` for every external series, distinct from its `event_at`.

**Publication-date leakage.** The most common form in a system like ours. A source dated after the backtest window "predicts" returns inside it — the correlation is real and completely spurious, because the knowledge that generated the signal could not have been acted on. Detection: compare `sources.date` against the experiment's `data_version` window; any source dated later than the window may motivate a hypothesis but may not justify a PASS for a backtest inside it.

**Timestamp mismatch.** Bar closes stamped by exchange-local time, by UTC, by a vendor's ingest time, and by a database `TIMESTAMPTZ` default are four different clocks. One second of skew at a bar boundary is enough to move a trade from one bar to the next. The schema already moved every provenance column to `TIMESTAMPTZ` for exactly this reason; the failure mode survives that. Detection: assert that every bar's `open_time < close_time <= decision_time` in one timezone, and require the ingestion path to carry the venue's clock rather than the ingest host's.

**Execution-time ambiguity.** "Buy when the condition fires" does not say when the buy happens. If the condition is evaluated on the close of bar *t* and the fill is assumed at the close of bar *t*, the fill uses information from the instant it was placed. Manifests as: a `Signal.horizon` that is `unspecified`, or a fill convention that is never written down. Detection: every `Signal` must carry a non-`unspecified` horizon, and the RuleSpec's signal condition must be expressible against bar-closing data only. This is the failure that most often turns a good hypothesis into an untestable one, because there is nothing to disagree about.

## 2. Statistical failures → `not_falsifiable`

These all produce the same outcome: the claim does not constrain anything, because it was selected to fit what was already seen. The mapping is `not_falsifiable` and not a dedicated "overfitting" value because the defect is epistemic, not computational — see the note after the list.

**Multiple testing.** Testing many variants and reporting the best. Ten variants at a 5% level produce a best result that clears 5% under the null roughly half the time. Detection: `research_identity.parameter_space` declares the space searched; the number of variants tried must be part of the artifact, and the reported threshold must be adjusted for it. A hypothesis whose `parameter_space` is `{}` while its `features` list has seven entries is hiding the count.

**p-hacking.** Choosing the test statistic, the sample window, or the exclusion rule after seeing the result. Detection: only count a specification as pre-registered if it exists as a `hypothesis_versions` row whose `creation_timestamp` precedes the run. Anything decided afterwards is a new hypothesis, not a corrected one.

**Data snooping.** Repeated inspection of the same test set until noise looks like signal. Detection: structural — Layer 2 must hold out data Layer 1 has never been shown, and Layer 1's artifacts must be frozen (`spec_sha256`) before queueing. The schema enforces the freeze: `hypothesis_versions` is append-only behind a trigger.

**Selection bias.** The sample was chosen for a reason correlated with the outcome — only periods where the effect was visible, only instruments that survived, only days with data quality problems that also had large moves.

**Overfitting.** Too many free parameters relative to the effective sample. A ten-parameter rule over six months of hourly crypto data describes the sample, not the market.

**Parameter mining.** The specific, common case: sweeping lookback lengths and thresholds, then keeping the best. This is overfitting performed by a script, and it is what "let the model search" produces automatically if nothing bounds it.

**Why `not_falsifiable` and not a separate value.** The claim is not false — it is *unconstrained*. It was manufactured from the noise in the sample rather than posited independently, so it agrees with the data no matter what the data says and disagrees with it in no world. A hypothesis that cannot lose is not a hypothesis; it is a description. Grouping these under `not_falsifiable` keeps the enum honest: there is exactly one way to be unfalsifiable, and a fitted parameter surface is the most common route to it.

## 3. Market failures → `not_measurable` / `not_falsifiable`

The user-supplied survey figures that motivate this section: of 77 studies surveyed, 19 were primary work; only 2 of those 19 used time-consistent splits; 1 of 19 had a transaction-cost model; 1 of 19 documented survivorship; 11 of 19 reported execution timing. Those counts come from the user's research notes, not from our own reading — treat them as directionally decisive, and treat any specific number quoted downstream as unverified until the source is in hand. The implication for this engine is concrete: **most published claims are not comparable to ours, because most of them were not measured under our constraints.**

**Survivorship bias.** Delisted, deleveraged and bankrupt instruments are absent from the data, so every backtest only ever saw the firms that survived. Detection: assert that the instrument universe is point-in-time; a universe reconstructed from a current listing table is a fail. Maps to `not_measurable` — the counterfactual cannot be constructed from what we have.

**Unrealistic liquidity.** Filling an order for a size the book cannot absorb at the quoted price. Detection: compare order notional against depth at the touch on the same bar; if size exceeds available depth, the hypothesis is not measurable at that size. Maps to `not_measurable`.

**Unrealistic slippage.** Assuming a fill at mid, or at the close of the bar that generated the signal, when the trade's own size moves the market. Maps to `not_measurable`.

**Ignored spread.** Crossing the spread costs roughly the spread; a hypothesis whose edge is smaller than the spread it must pay through has not been measured, only assumed. Detection: require the RuleSpec's market block to name the venue, so the spread is well-defined. Maps to `not_measurable`.

**Ignored fees.** Explicit maker/taker fees, funding, and borrow. In crypto perpetuals funding is not optional — it is a scheduled transfer between the two sides and it accrues to whoever holds the position. Maps to `not_measurable`.

**Regime dependence.** The effect exists in one volatility regime and not another. This is not a defect in the hypothesis; it is an *undeclared condition*. An undeclared regime dependence is `not_falsifiable`, because the claim as stated makes a prediction for all time and is therefore refuted by the very periods that motivate conditioning it. Declaring the regime moves it into `conditions` and makes it testable.

**Capacity constraints.** The relationship holds at 100k notional and dies at 10M because the mechanism that produced it was itself sized. Maps to `not_measurable` at the sizes actually intended.

## 4. AI-specific failures

These exist because a language model is in the loop. They split cleanly by whether the artifact is a *bad idea* or a *bad generation*.

**Training-data leakage.** The model recites a published result — correctly, usually — but recites it as if it were a fresh finding, so it enters the corpus with no `source_id` and no verifiability. Detection: any claim lacking a `source_evidence` entry. Maps to `implausible_mechanism` if the model also supplies a confident but empty rationale.

**Historical memory leakage.** The model knows what happened to a market and uses it as a mechanism. "Momentum works in crypto" is not a mechanism; it is a recollection of an outcome. Detection: ask why the mechanism should hold on an asset the model has never seen post-training. Maps to `implausible_mechanism`.

**Hallucinated source.** A citation to a paper that does not exist, or a real paper that says the opposite. Detection: `sources.content_sha256` must exist for anything cited as evidence; a claim with no stored source is not evidence. Maps to `implausible_mechanism` when it supplies the mechanism, `malformed_llm_output` when it is a stray citation attached to an otherwise sound claim.

**Hallucinated empirical result.** The model reports a number — a correlation, a Sharpe, a sample size — that no source contains. This is the most dangerous failure in the set because the artifact looks maximally complete. Detection: **every numeric result in a hypothesis must trace to a stored source or be marked as an estimate.** A hypothesis asserting "OFI explains 0.7 of return variance" with no `source_id` is `malformed_llm_output`, always. Layer 1 must never state an empirical result of its own; that is Layer 2's job, and only with data.

**Prompt instability.** The same prompt, same model, different hypothesis. The corpus fills with near-duplicates that are not marked as such. Detection: the `prompt_version` column is the mitigation — two artifacts generated under different prompt versions are not comparable. `not_distinct` when the near-duplicate already exists.

**Model-version drift.** The corpus's epistemic quality changes when the generating model changes underneath it, and nothing marks the boundary. Mitigation is the stored `generator_model` / `model_used` on every row; a comparison that spans a model change is not a like-for-like comparison.

**Reasoning inconsistency.** The stated mechanism does not imply the stated prediction. The artifact says "informed traders anticipate it" and then predicts the *opposite* sign of the flow variable. Detection: this is a semantic check with no programmatic shortcut, which is precisely why the critic is an LLM with a checklist rather than a threshold function. Maps to `implausible_mechanism`.

**Reward hacking → `not_measurable`, and the reason Layer 1 has no profit target.** This is the failure that motivates our central constraint. Any target in the research layer creates a gradient, and a gradient gets optimised. If the prompt says "produce hypotheses likely to make money", a capable model will produce claims shaped to flatter a backtest: effect sizes chosen to be large, mechanisms chosen for plausibility rather than truth, failure conditions omitted, conditions omitted so the claim can never be contradicted. The output is not a bad hypothesis — it is a well-formed, falsifiable-looking artifact with a hidden objective, and it is far harder to detect than an obvious hallucination. See [hypothesis_testing.md](hypothesis_testing.md) for the score that replaces the target, and [../frameworks/tradingagents.md](../frameworks/tradingagents.md) for the general shape of multi-agent drift.

## 5. Research failures

**Unfalsifiable hypothesis.** No possible observation refutes it: no horizon, no direction, no conditions. If a hypothesis cannot be wrong it is a story. Detection: the `HypothesisBlock.null` field must be a concrete observation, and there must be no grammatical escape hatch — "under the right conditions" and "to some extent" are refusals to fill in the field. Maps to `not_falsifiable`.

**Duplicate hypothesis.** The same claim, restated. Detection: `research_identity.source_ids` plus a comparison of `candidate_variables` and target horizon against existing hypotheses. The domain premise in `layer1/domains.py` exists partly to make these comparable — two hypotheses in `microstructure` share a universe and can be checked against each other; two across domains cannot. Maps to `not_distinct`.

**Post-hoc hypothesis.** Invented after the result was known, in the belief that naming an effect makes it found. This is p-hacking's narrative form. Detection: `hypothesis_versions.creation_timestamp` versus the run it claims to anticipate. Maps to `not_falsifiable`.

**Indicator soup.** A combination of indicators proposed because they are available, with no shared premise. `layer1/domains.py` names this failure as the one Moon Dev walked into by letting a model free-associate over every indicator ever named. Detection: can every feature be traced to the stated mechanism? A feature that cannot is decoration. Maps to `implausible_mechanism`.

**Undefined mechanism.** A prediction with no account of why it should hold. The claim might still turn out true, but nothing in the artifact tells us whether we learned something. Maps to `implausible_mechanism`.

**Undefined prediction horizon.** "It predicts" with no time scale. Maps to `not_measurable` — nothing to measure against.

**Confounded variables.** Two variables move together for a reason unrelated to the stated mechanism — volume and volatility, OI change and price change, funding and basis. The correlation is real; the mechanism is not what it appears to be. Detection: enumerate the alternative explanations and require the RuleSpec to distinguish them. Maps to `not_falsifiable` when the artifact leaves `alternative_explanations` empty, because then the hypothesis cannot survive a test that would separate it from its confound.

**Hidden discretion** deserves its own note because the enum has a dedicated value for it. Any artifact where an unrecorded human or model judgement picks the window, the threshold, the universe, or the exclusions has moved the decision out of the spec and into somebody's head. The result is irreproducible, because the discretionary choice is not written down anywhere. Detection: every parameter is in `RuleSpec`, and every parameter in `research_identity.parameter_space`. If a number influences the outcome and is not in the spec, the failure is `hidden_discretion`.

**Unformalizable** is the last enum value, and it is the one the Formalizer rather than the Critic produces. It means the artifact could not be turned into a deterministic `RuleSpec` at all: the signal condition cannot be written as an expression over named features, or a parameter has no computable definition, or the claim is self-contradictory so the two halves would formalize into different rules. Detection: the Formalizer's YAML fails to validate against `RuleSpec`, or validates only after the Formalizer has to invent a condition the hypothesis never stated — inventing the condition is itself the `unformalizable` signal, because the hypothesis did not contain enough specification to formalize. It is recorded at `stage = 'critic'` when caught before queueing.

---

## Negative knowledge

A research system that stores only successes will rediscover the same failed ideas forever, because each rediscovery arrives with fresh confidence and no memory of the last attempt. The fix is structural: **negative results are first-class records with the same standing as positive ones.**

The `failures` table is the mechanism. It carries `hypothesis_id`, `reason`, `failure_type`, `stage` (`critic` or `layer2_backtest`), `reported_by`, and `created_at`. It is written by two different parties on equal terms: the Research Critic when it rejects, Layer 2 when a backtest refutes. A failure is a result, not an error — the schema comment says so, and the `prior_failure_collision` checklist dimension exists to make sure the table is actually read before a hypothesis is admitted.

The record it holds, viewed as knowledge rather than as a log row:

```
mechanism            the economic mechanism that was tested
result               failed
conditions           market, regime, universe, horizon, data version under which it failed
failure_reason       the specific failure mode, as a failure_type value plus prose
confidence           how firmly the failure is established
```

`conditions` is what makes a failure reusable rather than merely recorded. "OFI didn't work" is useless. "OFI lost its predictive power once the cost regime in the sample exceeded its measured edge, at 1-minute aggregation, on the 2024 majors" tells the next generator exactly which sibling to avoid and which variant is still worth trying.

**"It didn't work" is not a reason.** The `reason` column must name the mode, not the outcome. A rejection whose reason is a restatement of the verdict has recorded nothing: the next generator will read it as closed and will not know what to do differently. Every `failures` row must be answerable to "what specifically about this artifact was wrong, and what would a different artifact need to do?" — which is the same question `prior_failure_precedent` asks before admitting a hypothesis.

Two properties are enforced by the schema rather than by convention, and both exist for negative knowledge specifically. `hypothesis_versions` is append-only behind a trigger, so a failure attached to a version cannot later be quietly argued away by editing the spec. And `sources` is append-only, so a source that produced a failed hypothesis cannot be removed to make the corpus look cleaner. The history of what did not work is as protected as the history of what did.