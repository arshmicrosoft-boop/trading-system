# HYPOTHESIS_SCHEMA

The field-by-field shape of a Layer 1 hypothesis. This is what the Hypothesis Generator produces, what the Research Critic checks, and what the Formalizer turns into a `RuleSpec`.

Related: [failure_modes.md](failure_modes.md) (what each field is checked for) · [hypothesis_testing.md](hypothesis_testing.md) (the five questions the fields answer) · [REPLICATION.md](REPLICATION.md) · [contradiction_resolution.md](contradiction_resolution.md) · [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) · [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md)

## The logical object

A hypothesis is a claim plus the conditions under which it is claimed, plus the observation that would refute it. Everything else is metadata about the claim.

```
hypothesis_id            stable identity
title                   one line, human-readable
research_question       what are we asking?
mechanism               why should this exist?
claim                   the assertive statement
prediction              what must specifically be observable, over what horizon
direction               bullish | bearish | neutral | conditional
features                the observable variables carrying the mechanism
target                  variable + horizon the claim is about
conditions              regime, universe, venue, state of the world
universe                domain the hypothesis lives in
data_requirements       what must be in the data for this to be testable
expected_failure_conditions   what would make us abandon it
alternative_explanations what else could produce this correlation
source_evidence         source_ids backing the idea
related_hypotheses      siblings, ancestors, conflicts
status                  lifecycle state
```

## Field by field

**`hypothesis_id`** — stable, content-independent identity. Every downstream artifact (failures, versions, queue rows, Layer 2 experiments) keys on it. Once a hypothesis has a version, its id must never change; a changed claim is a new hypothesis.

**`title`** — one line. Reads as a question or a claim, never as a trade. "Does order flow imbalance retain predictive power net of taker fees at 1-minute aggregation?" not "OFI taker scalp".

**`research_question`** — the question being asked, before it is answered. Distinct from `claim`: the question is neutral, the claim is a position on it. This field is what makes a hypothesis *about* something rather than a rule.

**`mechanism`** — why the relationship should exist. This is the field that distinguishes research from curve-fitting; see "the four load-bearing fields" below.

**`claim`** — the assertive statement. Must be wrong in some possible world.

**`prediction`** — the specific observable the hypothesis requires: which variable moves, in which direction, over what horizon, by what construction. "OFI predicts returns" is not a prediction. "The 5-level OFI over the trailing 60 seconds is positively associated with the next 5-minute return" is, and it is checkable.

**`direction`** — `bullish`, `bearish`, `neutral`, or `conditional`. Note `conditional` is a first-class value and not a cop-out: it is the correct direction for a hypothesis whose claim holds in some regimes and not others, provided `conditions` names the regimes. Layer 2 needs to know whether the artifact is a directional call or a conditional filter, because those require different evaluations.

**`features`** — the observable variables carrying the mechanism. Prefer names from the domain universes in `layer1/domains.py` (`microstructure`, `forced_flows`, `relative_value`, `price_volume`) so that hypotheses within a domain are comparable and duplication is detectable. Every feature must be traceable to the mechanism; one that is not is indicator soup.

**`target`** — the variable and the horizon the claim is about. Two parts, both required: a target without a horizon is `not_measurable`.

**`conditions`** — regime, universe, venue, and the state of the world under which the claim holds. A hypothesis with no conditions is making a prediction for all time, and will be refuted by the periods that should have conditioned it. See regime dependence in [failure_modes.md](failure_modes.md).

**`universe`** — the domain. Bounded on purpose. A hypothesis that cannot be placed in a domain is outside the corpus's model of the world and must justify why.

**`data_requirements`** — what the data must contain for the hypothesis to be testable at all: resolution, fields, history depth, venue coverage. Also the natural home for the negative requirement: if this needs a variable that exists only in a vendor product we do not have, say so now. `not_measurable` is cheapest to catch at generation time.

**`expected_failure_conditions`** — what would make us abandon it. See below; this is load-bearing.

**`alternative_explanations`** — what else could produce this correlation. See below; also load-bearing.

**`source_evidence`** — the `source_id`s backing the idea, plus a declaration when there are none and the hypothesis is novel. This is the field that makes duplication and hallucination detectable, and the field that [REPLICATION.md](REPLICATION.md) populates with an external anchor.

**`related_hypotheses`** — siblings, the hypothesis this one refined, and any hypothesis carrying a conflicting claim. This is the graph that makes refinement-over-invention navigable and that lets a contradiction be recorded rather than averaged away.

**`status`** — lifecycle: `proposed` → `critiqued` → (`rejected` | `formalized` → `queued`). A native `ENUM` in the schema, so the database cannot accept an unparseable state.

## The four load-bearing fields

Most fields are bookkeeping. Four of them do the actual work of forcing scientific thinking, and each is load-bearing for a specific reason.

### `mechanism` — why should this exist?

Without a mechanism, a hypothesis is an observed correlation plus a rule for trading it. The correlation might be real, might be a confound, might be an artefact of the sample — and the artifact provides no way to tell. The mechanism is what makes the claim generalisable beyond the sample it was noticed in: it should hold on an asset the author has never seen, because it does not depend on that asset's history.

It is also the defence against historical memory leakage. "Momentum works in crypto" is a recollection of an outcome wearing a mechanism's clothes. Asking why the mechanism should exist on an unseen asset is the question that distinguishes the two. Absent → `implausible_mechanism`.

### `prediction` — what specifically must be observable?

A claim without a specific prediction constrains nothing, because there is no observation that could count against it. "OFI has predictive power" is satisfied by any nonzero correlation and refuted by none. The prediction field forces the claim down to a checkable form: which variable, which direction, which horizon, which construction.

This is the load-bearing field for `not_falsifiable`, and it is what the Formalizer turns into `HypothesisBlock.signal` — a `condition` plus a `horizon`. Note that `Signal.horizon` defaults to `"unspecified"` in `layer1/contracts.py`; an unspecified horizon is precisely the execution-time ambiguity documented in [failure_modes.md §1](failure_modes.md), and a hypothesis that reaches formalization with one has failed its own prediction field.

### `alternative_explanations` — what else could produce this correlation?

This is the field that separates a mechanism from a confound, and it is the one most often left empty because filling it feels like arguing against yourself.

It is not. Volume and volatility move together for reasons that have nothing to do with any mechanism one might propose. Open interest change and price change are near-tautologically linked. Funding and basis are two views of the same quantity. If the hypothesis does not enumerate the alternatives and the design does not separate them, then a positive result is uninterpretable — the correlation is real either way, and nothing in the artifact tells us which explanation produced it.

An empty `alternative_explanations` is `not_falsifiable`, because the hypothesis cannot survive a test designed to separate it from its own confound.

### `expected_failure_conditions` — what would make us abandon it?

The field that most distinguishes research from promotion. A hypothesis that lists no conditions under which it should be dropped is making a commitment to be right rather than a statement about the world.

It is also the mechanism by which the research layer refuses to be an optimiser. This is where a return target would do its damage if one were present: a hypothesis written to hit a target states failure conditions that are absent or decorative, because failure conditions are exactly what a target-seeking writer has no reason to include. Filling this field honestly is the practice that makes the no-profitability-target rule operational rather than aspirational. See [hypothesis_testing.md](hypothesis_testing.md).

It doubles as negative knowledge generation: when a hypothesis does fail, `expected_failure_conditions` is what tells the next generator whether this was the mechanism failing or one of its declared caveats biting.

## Forbidden fields

**These may never appear in a Layer 1 hypothesis, in any form, at any nesting depth:**

- `expected_return`
- `target_return`
- profit target
- Sharpe target
- position sizing

**Why.** These are optimisation objectives, not claims about the world. A hypothesis carrying one is a strategy in disguise that has skipped the falsification step — it asserts a desired outcome rather than a mechanism, and the desired outcome is what the artifact then gets evaluated against.

Worse, they are not neutral omissions. A target is a gradient, and a gradient in the research layer gets optimised. A generator told to produce hypotheses near a return target will produce claims shaped to flatter a backtest: effect sizes chosen large, mechanisms chosen for plausibility rather than truth, `conditions` omitted so nothing can contradict the claim, `expected_failure_conditions` left empty. Every one of those is a well-formed artifact, and together they are much harder to detect than an obvious hallucination. This is reward hacking, catalogued in [failure_modes.md §4](failure_modes.md), and it is the reason Moon Dev's `TARGET_RETURN = 50` is rejected rather than merely set to a different value.

**This is structurally enforced, not merely discouraged.** `RuleSpec` and every nested block set `model_config = ConfigDict(extra="forbid")` in `layer1/contracts.py`, so an unrecognised key is a validation error rather than a stored field. `expected_return` is therefore not "a field we tell the model to avoid" — it is unrepresentable in the formalized artifact. The `research_identity` table agrees: it carries `parameter_space` and provenance but deliberately has no `expected_return` column, and the schema comment says so.

If you find yourself wanting to add such a field to capture something, you want `prediction` or `expected_failure_conditions` instead.

## What is persisted versus prompt-only

An engineer reading this needs to know which of the above fields exist as columns and which exist only as generator guidance, because the two are enforced very differently.

**Persisted as columns** (`db/schema.sql`):

| field | column |
|---|---|
| `hypothesis_id` | `hypotheses.hypothesis_id` |
| `claim` | `hypotheses.statement` |
| `mechanism` | `hypotheses.expected_mechanism` |
| `features` | `hypotheses.candidate_variables` (`TEXT[]`) |
| `universe` | `research_notes.domain` on the parent mechanism |
| `status` | `hypotheses.status` (native `ENUM`) |
| `source_evidence` | `research_identity.source_ids` (`TEXT[]`) |
| — generator identity | `hypotheses.generator_model`, `hypotheses.prompt_version` |

**Persisted inside the formalized spec** (`hypothesis_versions.yaml_spec`, validated by `RuleSpec`):

| field | where in the spec |
|---|---|
| `claim` | `hypothesis.statement` |
| `prediction` | `hypothesis.signal.condition` + `hypothesis.signal.horizon` |
| falsification criterion | `hypothesis.null` |
| `features` | `features` (`min_length=1`) |
| `universe` | `market.universe` |
| `mechanism` | `expected_mechanism` |
| `status` | `status` |

**Prompt-only guidance — generated, checked by the critic, but not a column:**

`title`, `research_question`, `direction`, `conditions`, `data_requirements`, `expected_failure_conditions`, `alternative_explanations`, `related_hypotheses`.

**Consequences of that split.** Four fields are strongly protected by structure: `claim`, `mechanism`, and `prediction` reach `RuleSpec` and are hash-frozen in `spec_sha256`, so they cannot be quietly edited after generation — `hypothesis_versions` is append-only behind a trigger. And `null` is `min_length=1` in `HypothesisBlock`, so an empty falsification criterion is a validation failure.

The rest is weaker, and honestly so: `alternative_explanations` and `expected_failure_conditions` are among the most important fields in this document and are **prompt-only today**. They are enforced by the Research Critic reading them, and by the `implausible_mechanism` / `not_falsifiable` checklist dimensions. Nothing in the schema prevents an artifact with both empty. That is the current state of the system, recorded here honestly rather than papered over; if enforcement matters, the schema is where it goes — and it is not Layer 1's job to decide that unilaterally.

`direction` is likewise prompt-only, which is why `Signal.notes` exists on the spec: it is the escape hatch where a conditional direction and its conditions can be carried into the formalized artifact without adding a column.

## Epistemic status

Our own operational contract, derived from the pipeline's actual Pydantic models and SQL schema. The field list is authoritative as a description of what the system expects; the logical grouping and the four-load-bearing-fields argument are our own framing.

Two caveats for a future agent. First, the mapping table above reflects the schema as written — if `hypotheses` or `RuleSpec` changes, this document must be updated in the same change, because a stale schema description is worse than none. Second, the four prompt-only fields are a known enforcement gap, not an oversight to be quietly written around. If you find yourself relying on one of them being enforced structurally, it is not, and the enforcement lives in the critic's judgement.