# RESEARCH INTEGRITY CHECKLIST

**This is the operative artifact.** The Research Critic and the Research Integrity Checker
consume this file. Items are machine-evaluable: each has a stable `id`, a deterministic
detection method wherever one exists, and an explicit verdict mapping onto
`CritiqueDimension` / `FailureType` / `hypothesis_status`.

Supporting reading: [reproducibility_audit.md](reproducibility_audit.md) (why protocol
completeness is binary), [knowledge_leakage.md](knowledge_leakage.md) (epistemic typing and
point-in-time rules), [hypothesis_discovery_loop.md](hypothesis_discovery_loop.md) (lineage and
mutation typing).

## Scope and the standing constraint

This checklist scores **research quality only**: is the mechanism stated, is the claim
falsifiable, is it novel, is it measurable, is it temporally honest, is confounding addressed,
was it formed before seeing the result, and is the rule implementable unambiguously.

**No item in this checklist, and no score derived from it, refers to trading performance.**
There is deliberately no target return, no profit threshold, and no "is this strategy good"
question anywhere in Layer 1. A hypothesis that cannot be measured and cannot be falsified is
rejected on rigor grounds, whatever it might do. This is the deliberate departure from Moon
Dev's implementation, whose `TARGET_RETURN = 50` and `SAVE_IF_OVER_RETURN = 1%` let the
research layer optimise toward a number instead of toward truth — see
[../moondev/rbi_methodology.md](../moondev/rbi_methodology.md).

## Verdict semantics

Two distinct non-PASS outcomes, deliberately not merged:

| Outcome | Meaning | Schema effect |
|---|---|---|
| `PASS` | Every item below returned PASS or an accepted `justified` flag. | Eligible for `formalized` -> `queued`. |
| `INCOMPLETE` | A **required protocol field is missing or UNDEFINED**. The hypothesis is unfinished, not wrong. | Status stays `critiqued`. **Never** `formalized`, **never** `queued`. Recoverable: fill the field and re-check. |
| `FAIL` | The hypothesis itself is defective — unmeasurable, unfalsifiable, ambiguous, leaked, duplicated, causally unsupported. | `Critique(verdict="FAIL", failure_type=..., scores{...0})`; `hypotheses.status = 'rejected'`; a `failures` row is written so the family is not re-proposed. |

`Critique` validation already enforces that a FAIL names a `failure_type` and a PASS carries
none, and that per-dimension scores are 0-2 (0 = fail, 1 = weak, 2 = sound).

## Machine-evaluable checklist definition

```yaml
integrity_checklist_version: 1

items:
  - id: LOOKAHEAD_INFORMATION
    dimension: no_future_leakage
    detection: [rule_based, protocol_field, llm_probe]
    non_pass: [FAIL, INCOMPLETE]

  - id: AMBIGUOUS_RULE
    dimension: measurable            # implementation ambiguity defeats measurability
    detection: [double_formalization_divergence, hedge_lexicon, unfrozen_parameter]
    non_pass: [FAIL]

  - id: IMPOSSIBLE_TO_SOURCE_VARIABLE
    dimension: measurable
    detection: [variable_registry_lookup, provenance_check]
    non_pass: [FAIL]

  - id: HIDDEN_DISCRETIONARY_DECISION
    dimension: no_hidden_discretion
    detection: [discretion_lexicon, unbounded_quantifier, parameter_space_closure]
    non_pass: [FAIL]

  - id: UNDEFINED_TIME_HORIZON
    dimension: falsifiable
    detection: [horizon_parse, horizon_data_range, signal_execution_ordering]
    non_pass: [FAIL, INCOMPLETE]

  - id: UNDEFINED_UNIVERSE
    dimension: measurable
    detection: [protocol_field]
    non_pass: [INCOMPLETE]

  - id: DUPLICATE_HYPOTHESIS
    dimension: distinct
    detection: [spec_hash_equality, statement_similarity, lineage_reachability, failures_collision]
    non_pass: [FAIL]

  - id: UNSUPPORTED_CAUSAL_CAIM
    dimension: mechanism_plausible
    detection: [mechanism_causal_connective, claim_type_declaration, claim_type_promotion_check]
    non_pass: [FAIL]

  - id: CONTAMINATED_HISTORICAL_INFORMATION
    dimension: no_future_leakage
    detection: [future_information_risk_flag, masking_attestation, point_in_time_check]
    non_pass: [FAIL, INCOMPLETE]

verdict_rule:
  any_item_FAIL:            FAIL
  any_item_INCOMPLETE:      INCOMPLETE
  all_items_PASS:           PASS
  FAIL_and_INCOMPLETE_together: FAIL   # the stronger verdict wins
```

## The nine items

### 1. LOOKAHEAD_INFORMATION — *does the hypothesis use information that was not available at decision time?*

**Why it matters.** This is the failure that makes every other integrity guarantee
decorative. The survey's funnel (2 of 19 primary studies reporting extractable time-consistent
splits, in [reproducibility_audit.md](reproducibility_audit.md)) shows that most of the field
cannot even state when the decision was made — so this failure is usually *invisible to the
author*, not merely committed.

**Detection.**
- *Protocol field:* `temporal_protocol.{train,validation,test}` all present, with
  `train.end < validation.start <= test.start <= test.end`; `purge_embargo_hours >= 0` where
  labels overlap the split boundary.
- *Ordering rule:* `execution.signal_time` must strictly precede `execution.execution_time`,
  and both must be resolvable to distinct bars. `signal_time == "close of bar k"` with
  `execution_time == "close of bar k"` is an automatic FAIL.
- *Variable audit:* every `candidate_variable` must have a stated computation window, and the
  window's right edge must precede the signal time. Any variable described as "confirmed",
  "realised", "final", "settled", "at month end", "after the close", or "next session" is
  flagged for manual confirmation.
- *LLM probe (secondary):* ask the generator to state the exact timestamp at which each
  variable is first knowable. A non-instantaneous answer is a FAIL.

**FAIL maps to:** dimension `no_future_leakage`, `failure_type = future_information_leak`,
status `rejected`. **INCOMPLETE** (rather than FAIL) when the temporal block is missing or a
split boundary is UNDEFINED — see item 6's note that INCOMPLETE is never a queue path.

### 2. AMBIGUOUS_RULE — *could two competent engineers implement this differently?*

**Why it matters.** The corpus stores a formalised spec and derives a content hash
(`spec_sha256`). If the prose admits several implementations, the hash is a hash of an
accident, and every downstream reproducibility claim attached to that spec is void.

**Detection.**
- *Double formalization:* formalise the hypothesis twice through the Formalizer in separate
  calls; if the two `yaml_spec` payloads differ on any decision-relevant field, this is a FAIL.
  This is the strongest available check and it is fully automatable.
- *Hedge lexicon:* "typically", "usually", "generally", "significantly", "substantially",
  "materially", "in most cases", "roughly", "approximately" attached to a decision that
  changes the trade.
- *Unbound terms:* "extreme", "unusual", "abnormal", "high volume", "large" with no numeric
  threshold or percentile.
- *Unfrozen parameters:* any constant in the statement that is neither in
  `research_identity.parameter_space` nor written as an explicit literal with units.

**FAIL maps to:** dimension `measurable`, `failure_type = unformalizable`, status `rejected`.
Disambiguation is always attempted by the Formalizer first — a hypothesis that only needs its
threshold written down is not a rejection.

### 3. IMPOSSIBLE_TO_SOURCE_VARIABLE — *can this variable actually be computed from data that exists?*

**Why it matters.** Hypotheses routinely name things no vendor publishes. They formalise
cleanly, queue cleanly, and then die in Layer 2 with "column not found" — or worse, get
quietly substituted with the nearest available proxy, and the corpus records a claim about a
variable nobody measured.

**Detection.**
- *Registry lookup:* every `candidate_variable` must appear in `layer1.domains.ALL_VARIABLES`,
  or carry a `justification` explaining why it belongs to the mechanism's domain universe.
  The domain docstring is explicit that these are seed universes and enforcement is a
  justification duty, not a whitelist.
- *Provenance check:* the variable must resolve to a computable quantity from
  `data.provider` / `dataset_version` with a stated computation window. Anything requiring a
  proprietary, undisclosed, or post-hoc-computable series is a FAIL.
- *Sign check:* variables whose sign is fixed by definition (a spread is non-negative, funding
  is annualised) must be flagged for review when the proposed rule treats them as signed.

**FAIL maps to:** dimension `measurable`, `failure_type = not_measurable`, status `rejected`.

### 4. HIDDEN_DISCRETARY_DECISION — *does the rule contain a judgement call?*

**Why it matters.** Discretion is unlogged parameter fitting. A rule with one discretionary
step has one extra hidden degree of freedom, and a backtest that tunes it is a backtest that
searched. This is exactly how a hypothesis becomes a description of the past.

**Detection.**
- *Discretion lexicon:* "avoid", "if conditions suggest", "take profit when", "stop when
  appropriate", "hold until", "adjust for", "manually", "as needed", "filtered".
- *Unbounded quantifiers:* "any", "some", "most", "several" applied to instruments or periods.
- *Parameter-space closure:* every threshold, cap, floor and time constant must appear
  literally in the spec or inside `parameter_space`. A constant that exists only in the
  author's head is a FAIL.
- *Target mismatch:* the rule must not be "trade the things this hypothesis predicts" — a
  universe selected by the hypothesis's own signal is selection on the tested variable.

**FAIL maps to:** dimension `no_hidden_discretion`, `failure_type = hidden_discretion`, status
`rejected`.

### 5. UNDEFINED_TIME_HORIZON — *is the horizon stated, numeric, and resolvable?*

**Why it matters.** Without a horizon a prediction is unfalsifiable — there is always some
window in which it was right. It also makes the measurement in item 9 impossible and lets
Layer 2 silently pick a horizon after seeing the data.

**Detection.**
- *Horizon parse:* the statement must yield a positive integer horizon with explicit units
  (bars, minutes, hours, sessions). "Next time", "eventually", "short-term" fail.
- *Horizon data range:* the horizon must be shorter than the test window and resolvable from
  available bar resolution.
- *Label/split consistency:* with `purge_embargo_hours`, the embargo must be at least the label
  window, or the training set can see the label's future.
- *Interval spec:* the horizon must be explicit about whether it is measured on bars, wall
  clock, or sessions — crypto trades continuously, and "1 day" is ambiguous between 24h and
  the venue's daily boundary.

**FAIL maps to:** dimension `falsifiable`, `failure_type = not_falsifiable`, status `rejected`;
**INCOMPLETE** when the horizon field exists but is UNDEFINED.

### 6. UNDEFINED_UNIVERSE — *what is in the sample, and what happened to the assets that died?*

**Why it matters.** 1 of 19 primary studies in the survey documented universe/survivorship
handling. The universe is the largest silent degree of freedom in a trading backtest: it
determines which instruments were tradeable, and dropping the dead ones is a free, large,
one-directional return.

**Detection.** Pure protocol-field check, no LLM needed: `universe.assets` non-empty **or**
`universe.inclusion_rule` concrete; `survivorship_policy` present and one of
`delisted_retained` / `delisted_excluded` / `point_in_time_index`;
`universe.inclusion_rule` must not reference future attributes ("assets that later become
illiquid", "those that fall 50%"). Any `UNDEFINED` here is INCOMPLETE.

**INCOMPLETE maps to:** status remains `critiqued`, never queued. There is no FAIL variant for
a merely unspecified universe — the instrument list is a decision someone else must make, and
Layer 1 does not own it (see [reproducibility_audit.md](reproducibility_audit.md)).

### 7. DUPLICATE_HYPOTHESIS — *has this already been proposed, in any generation?*

**Why it matters.** A duplicate consumes a backtest slot and inflates the corpus with
apparent breadth. A duplicate of something already in `failures` is worse: it is the
generator ignoring its own recorded evidence, and it is how a research program spends its
budget re-testing a refuted mechanism seven times.

**Detection.**
- *Hash equality:* identical `spec_sha256` across `hypothesis_versions`.
- *Statement similarity:* normalised (lowercased, mechanism-stopword-stripped) statement
  similarity above threshold, with a differing-variable check to distinguish a real duplicate
  from a genuine sibling.
- *Lineage reachability:* if the candidate is reachable from an existing node by a declared
  mutation type, it is a REFINEMENT/GENERALIZATION/SPECIALIZATION and must be typed as such in
  `lineage` — an untyped duplicate of an existing node is a lineage error (see
  [hypothesis_discovery_loop.md](hypothesis_discovery_loop.md)).
- *Failures collision:* an existing `failures` row with the same mechanism and a reason that
  the candidate does not address. "Prior failure precedent" is a distinct `failure_type`
  (`prior_failure_precedent`) from a plain duplicate (`not_distinct`).

**FAIL maps to:** dimension `distinct` (plus `no_prior_failure_collision` when the duplicate is
of a recorded failure), `failure_type = not_distinct` or `prior_failure_precedent`, status
`rejected`.

### 8. UNSUPPORTED_CAUSAL_CAIM — *is the causal claim stated, typed, and backed by something other than itself?*

**Why it matters.** The four-way distinction in [knowledge_leakage.md](knowledge_leakage.md) is
useless unless the corpus enforces the types. Without typing, a source's assertion becomes a
"fact" the next time it is read, and a model's own inference becomes a citation.

**Detection.**
- *Causal connective:* `expected_mechanism` must contain a causal path — actor, action,
  consequence ("levered longs pay funding, forced unwinding sells"). A restatement of the
  prediction ("funding predicts reversal") is a FAIL. This is the check that keeps the
  attribution key usable by Layer 2.
- *Claim type declaration:* every extracted claim carries `fact | author_claim |
  empirical_result | model_inference`. Untyped claim = FAIL.
- *Promotion check:* a claim stored as `fact` whose supporting text is an assertion about a
  relationship is a promotion error, and is downgraded to `author_claim` with the source
  attached rather than accepted.
- *Inference isolation:* any `model_inference` claim used as motivation is attributed to
  `model_used` + `prompt_version` and can never be cited as a source finding.

**FAIL maps to:** dimension `mechanism_plausible`, `failure_type = implausible_mechanism`, status
`rejected`. Type-missing and promotion errors map to `unformalizable` with the item still
recorded against `mechanism_plausible`.

### 9. CONTAMINATED_HISTORICAL_INFORMATION — *did the reasoning agent already know the answer?*

**Why it matters.** This is the leak that survives every protocol check: the model recognised
the ticker, the date and the event, and produced a fluent analysis that is recall. A protocol
can be perfect and the result still measure memory (KTD-Fin, see
[knowledge_leakage.md](knowledge_leakage.md)). Protocol checks cannot detect it; only
provenance rules can prevent it.

**Detection.**
- *Flag:* `sources.future_information_risk = true` for any source describing realised outcomes
  without an excluding method.
- *Usability rule:* such a source may supply a **mechanism**, never a timing rule. If the
  hypothesis's decision rule depends on knowing the outcome, FAIL.
- *Masking attestation:* any generator task whose prompt contains a historical window carries
  `identifiers_masked = true` and `calendar_masked = true`. Absence of the attestation on such
  a task is a FAIL — the default assumption is contamination, and contamination is expensive to
  detect after the fact and cheap to prevent.
- *Point-in-time:* `information_available_at` must not postdate the earliest point at which the
  hypothesis's claim is asserted. Missing `information_available_at` is INCOMPLETE.
- *Outcome-shaped language:* phrases naming a specific historical episode ("as in the March
  squeeze", "like the last cascade") in a rule that is supposed to be time-general.

**FAIL maps to:** dimension `no_future_leakage`, `failure_type = future_information_leak`, status
`rejected`; **INCOMPLETE** when the point-in-time fields are UNDEFINED.

## The critic's eight questions

These are asked of every hypothesis and answer directly into `Critique.scores` (0-2 per
dimension). They are research-quality questions; none of them asks how much money the idea
makes.

1. **Mechanism** — is there a stated causal path, and is it plausible for this venue and this
   participant base? A prediction without a mechanism cannot be generalised, diagnosed, or
   partially salvaged when it fails.
2. **Falsifiability** — what observation would prove this wrong, and is that observation
   reachable with data we will actually have? If no outcome could refute it, it is not a
   hypothesis.
3. **Novelty** — what does the corpus already know that this does not do? Novelty is measured
   against the research tree, not against the world's literature; a rediscovered mechanism under
   a new name is not novel.
4. **Measurement** — can every named variable be computed from a named dataset at a stated
   resolution, with the label horizon inside the available window?
5. **Temporal integrity** — at the instant the rule fires, what exactly is known, and is any of
   it knowable only because the window has already closed?
6. **Confounding** — could the predicted return come from market beta, a style or factor
   exposure, a favourable regime, or the universe definition, rather than from the claimed
   mechanism? (See the attribution lesson in [knowledge_leakage.md](knowledge_leakage.md).)
7. **Data mining** — **was this hypothesis formed after seeing a result?** A rule proposed
   because it worked on a period already inspected is a description, not a prediction. The
   lineage block's `revision_reason` is where this is declared: a REPLICATION must carry no
   specification change, and any "it looked good on X" is a REFINEMENT whose evidentiary
   standing is nil.
8. **Implementation ambiguity** — could two engineers, working from this text alone, produce
   different trading rules? If yes, the spec is not a spec.

## Verdict mapping (summary)

| Item | Dimension | `failure_type` | Non-pass | Schema effect |
|---|---|---|---|---|
| LOOKAHEAD_INFORMATION | `no_future_leakage` | `future_information_leak` | FAIL / INCOMPLETE | `rejected` / stays `critiqued` |
| AMBIGUOUS_RULE | `measurable` | `unformalizable` | FAIL | `rejected` |
| IMPOSSIBLE_TO_SOURCE_VARIABLE | `measurable` | `not_measurable` | FAIL | `rejected` |
| HIDDEN_DISCRETIONARY_DECISION | `no_hidden_discretion` | `hidden_discretion` | FAIL | `rejected` |
| UNDEFINED_TIME_HORIZON | `falsifiable` | `not_falsifiable` | FAIL / INCOMPLETE | `rejected` / stays `critiqued` |
| UNDEFINED_UNIVERSE | `measurable` | — | INCOMPLETE only | stays `critiqued`, never queued |
| DUPLICATE_HYPOTHESIS | `distinct` | `not_distinct` / `prior_failure_precedent` | FAIL | `rejected` |
| UNSUPPORTED_CAUSAL_CAIM | `mechanism_plausible` | `implausible_mechanism` / `unformalizable` | FAIL | `rejected` |
| CONTAMINATED_HISTORICAL_INFORMATION | `no_future_leakage` | `future_information_leak` | FAIL / INCOMPLETE | `rejected` / stays `critiqued` |

Gate to the experiment queue: `verdict = PASS` **and** every protocol field in the five-block
contract concrete. A row in `experiment_queue` requires a `research_id`, which requires
`research_identity.data_version NOT NULL` — so an INCOMPLETE hypothesis cannot reach Layer 2
even if a bug let it past this file.

## What we deliberately did NOT include, and why

- **No performance criterion.** No item, score or threshold refers to expected or realised
  return. A profitability target would let hypotheses be selected for flattering a scoring
  function rather than for being true.
- **No partial credit for protocol completeness.** Four of five blocks filled is not "mostly
  reproducible"; it is unreproducible, and the survey's funnel says most published work in this
  field is exactly that.
- **No statistical power or significance thresholds.** Layer 1 does not test anything; it
  decides whether testing is meaningful. Power analysis is Layer 2's problem, and adding a
  threshold here would create a backtest target by the back door.
- **No automated score aggregation.** There is no "8.5/9, therefore queue". A single FAIL
  blocks. Averaging integrity dimensions lets a strong mechanism buy a look-ahead.

## Epistemic status

The checklist items, detection methods and verdict mappings are **our own design**, built from
the failure taxonomy we extracted; they are not a reproduction of any published review protocol
and no item cites a paper as its authority. The motivating statistics come from the
**Agentic Trading survey (arXiv 2605.19337), user-supplied and unverified by us** — 77 studies
audited, 19 meeting primary empirical criteria, 2 of 19 with extractable time-consistent splits,
1 of 19 with an explicit transaction-cost model, 1 of 19 documenting universe/survivorship
handling, 11 of 19 reporting execution timing/semantics, none reaching the highest
reproducibility level. Item 9 draws on the **KTD-Fin (arXiv 2605.28359)** contamination failure
mode, also user-supplied. Detection methods marked as lexicon or heuristic checks are proposals
with known false-positive rates and should be treated as triage aids that route cases to a
human or to the critic's judgement, never as proof of absence of a defect.

Related: [reproducibility_audit.md](reproducibility_audit.md),
[knowledge_leakage.md](knowledge_leakage.md),
[hypothesis_discovery_loop.md](hypothesis_discovery_loop.md),
[../moondev/controlled_experiment_design.md](../moondev/controlled_experiment_design.md).