# FinRobot — models reason, software computes, agents orchestrate, systems verify

## Purpose

AI4Finance's FinRobot contributes a **division of labour between nondeterministic
reasoning and deterministic computation**, and the discipline to record which side of
the line any given operation falls on. Its formulation, in substance: models reason,
software computes, agents orchestrate, systems verify.

That is a methodology reference for us. It is **not** evidence about markets, and it is
**not** a system we are adopting at FinRobot's scope. FinRobot is a financial analyst
that produces equity research reports; we are building a research engine that produces
falsifiable hypotheses and never touches an exchange. We borrow the reasoning/
computation boundary. We take none of the analyst scope.

## Key principle: four roles, one boundary

The formulation is useful because each clause excludes the others:

- **Models reason.** A language model is for interpretation, generation and judgement —
  reading a source and extracting a mechanism, turning a mechanism into a hypothesis,
  writing a structured spec. These require semantic understanding that no rule engine
  provides.
- **Software computes.** Anything decidable is decided by code, never by asking a model.
  Schema validity, date sanity, variable computability, duplicate detection, hashing,
  enum membership. Code is deterministic, testable, and cheap; a model is none of those.
- **Agents orchestrate.** Sequencing, retries and hand-offs are the agent's job: call the
  extractor, call the generator, call the formalizer, call the critic. Orchestration is
  control flow, and control flow belongs in deterministic code with an LLM as a called
  function — not in the model's head.
- **Systems verify.** No model output is admitted because the model said it was fine.
  Verification is an independent check that can fail the artifact.

FinRobot also tracks provenance: which model produced which artifact, under which
configuration. Without that, a hypothesis cannot be reproduced or retracted coherently
months later.

## Direct consequences for Layer 1's architecture

This principle governs our Layer 1 almost completely, and it maps onto code that
already exists.

### What the LLM is allowed to do — exactly three things

1. **Interpret a source** and propose mechanisms (`Research Extractor`).
2. **Generate hypotheses** from a mechanism (`Research Generator`).
3. **Write a structured spec** expressing the claim in the formal language
   (`Research Formalizer`).

And nothing else. The extractor is a good illustration: its system prompt embeds
`domains.describe_domains()` — the four universes with their shared economic premises —
and it hard-forbids trade recommendations, buy/sell language, entry/exit, position
sizing and profitability claims. The prompt states the boundary; the rest of the system
enforces it structurally.

### The non-negotiable corollary

**An LLM must never be trusted to perform deterministic validation itself.**

This is the single most important engineering consequence in this document, and it is
the one that is easiest to get wrong, because asking a model to "double-check your
output" feels like diligence and is not. A model asked to verify its own work reasons
about plausibility, not about the actual constraint; it has no incentive to fail, and
its failures are correlated with its own errors rather than independent of them.

The corollary has a hard architectural implication: **when a rule matters, it must be
a type, a constraint or a trigger — not a sentence in a prompt.** A prompt instruction
is a suggestion with a success rate. Enforcement belongs in code that the model cannot
influence.

We see this directly in `layer1/contracts.py`:

```python
class RuleSpec(BaseModel):
    """The machine-readable YAML contract handed to Layer 2.

    Layer 1 has no notion of a good return.  `model_config.extra='forbid'`
    plus the validator below make an expected_return field unrepresentable.
    """
    model_config = ConfigDict(extra="forbid")
```

`extra="forbid"` is the whole argument in one line. If a profitability target were a
prompt instruction, some future prompt edit, model update or careless regeneration
would eventually let one through. Because it is a Pydantic config, an `expected_return`
field is **unrepresentable** — the LLM cannot emit it into existence, because the
validator rejects the payload before any of our code sees it. The same `extra="forbid"`
discipline applies to `Signal`, `HypothesisBlock` and `MarketBlock`.

The codebase applies the principle consistently:

- **Enums over free text.** `source_type`, `source_tier`, `hypothesis_status`,
  `experiment_status` and `failure_type` are native PostgreSQL enums, so the database
  "cannot accept an unparseable lifecycle state that Layer 2 would have to defend
  against." A model proposing `status: "backtested"` is rejected by the column type.
- **Database triggers over application discipline.** `sources` and
  `hypothesis_versions` are UPDATE-blocked by trigger; `experiment_queue` freezes its
  identity columns at enqueue. Immutability is guaranteed below the application, so no
  bug — and no hallucinating agent — can route around it.
- **Structural refusal.** The `Critique` verdict is a `PASS`/`FAIL` literal and a
  `model_validator` enforces the internal consistency of the verdict and its failure
  type. A FAIL with no failure reason does not validate.
- **The queue invariant is checked twice.** `queueing.py`'s `_assert_queueable`
  refuses to enqueue anything that has not passed the critic, and states in its own
  docstring that it makes **two independent refusals because either alone is
  defeatable**. `_assert_provenance_agrees` then verifies that the hypothesis, version,
  research id and rule version being joined actually refer to one thing. A model cannot
  assert its way past either.
- **Provenance is structural.** `research_identity` requires `model_used`,
  `prompt_version`, `data_version`, `rule_version` and `feature_set` as `NOT NULL`
  columns, and `hypothesis_versions` carries `spec_sha256` plus
  `UNIQUE (hypothesis_id, spec_sha256)`. Reproducibility is a row that cannot be
  written incompletely.
- **Parsing is isolated.** `llm.py` contains `_extract_json`, the single boundary where
  a model string becomes data, raising `LLMError` on an unterminated object rather than
  passing a partial parse downstream. Model output never reaches the database unparsed.

The general rule a contributor should carry away: **if you can write a check as a
constraint, do not write it as an instruction.**

## What we deliberately did NOT copy, and why

- **Not the financial-analyst scope.** FinRobot's product surface is analyst-facing:
  company reports, valuation opinions, analyst sentiment. Layer 1's surface is
  hypotheses, formal specs and an experiment queue. Adopting the analyst framing would
  import a conclusion-first posture into a layer whose entire job is to avoid
  pre-supplying conclusions.
- **Not its agent role taxonomy.** FinRobot's agents are organised by *financial role*
  (analyst, researcher, trader). Ours are organised by *pipeline stage*
  (collector, extractor, generator, formalizer, critic, queueing). Stage-organisation is
  what makes our modules individually testable; role-organisation keeps the reasoning
  but weakens the seams. See [qlib.md](qlib.md).
- **Not tool-approval or reporting machinery.** FinRobot's human-in-the-loop approval
  patterns belong to systems that act on markets. Our hand-off is the
  `experiment_queue_layer2` view; Layer 2 is the separate system that consumes it.
- **Not its multi-agent debate structures for their own sake.** A debate between models
  produces more text, not more knowledge. We would rather have one structured spec plus
  a critic applying typed `failure_type` verdicts.
- **Not any performance claim from FinRobot's example reports.** Those are outputs of
  models reasoning about securities, which is precisely the class of output Layer 1 is
  designed not to produce.

## Epistemic status

**Secondary, architectural.** The four-clause formulation comes from the user's supplied
research notes on FinRobot; I did not read the AI4Finance/FinRobot repository or its
papers in this session. No file paths, version numbers, quotations, or benchmark figures
from FinRobot are asserted.

A future agent should treat the *separation principle* as settled guidance — it is
corroborated by our own code, which is where the concrete weight is — and should treat
any claim about FinRobot's internals as unverified unless someone has actually read the
source. Per
[methodology_references.md](methodology_references.md), FinRobot sits at **T2, author's
technical repository**: strong evidence about how its authors built a system, no
evidentiary standing about markets.

## Related

- [qlib.md](qlib.md) — the modularity that makes this division enforceable at module boundaries
- [tradingagents.md](tradingagents.md) — point-in-time evidence, the same "verify in code" discipline applied to time
- [methodology_references.md](methodology_references.md) — where FinRobot sits in the tier hierarchy
- [../methods/HYPOTHESIS_SCHEMA.md](../methods/HYPOTHESIS_SCHEMA.md) — the structured spec the LLM is allowed to write, and nothing more
- [../research_integrity/reproducibility_audit.md](../research_integrity/reproducibility_audit.md) — why provenance columns are `NOT NULL`
- [../moondev/ai_agent_architecture.md](../moondev/ai_agent_architecture.md) — Moon Dev's agent layering for comparison