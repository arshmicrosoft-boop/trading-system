# RBI Methodology: Research, Backtest, Implement

Source repository: Moon Dev's "Algorithmic Trading with AI" (Harvard project framing).
Tier: primary source **for Moon Dev's methodology**, not for the profitability of anything he trades.

## Purpose

This document captures the loop Moon Dev organises his work around — Research, Backtest,
Implement — and the five principles that make that loop mean something rather than being
a slogan for "write code, test it, ship it". The loop matters to us for exactly one
reason: it puts the burden of proof in a specific place. A strategy is not allowed to
exist until there is a stated, falsifiable claim about why it should work, expressed in
variables that can actually be observed in market data.

What this document is **not** evidence of: it is not evidence that any strategy described,
referenced, or implied in that repository works. It is not evidence about markets at all.
It is evidence about *process*. An engineer or agent reading this file should come away
able to make a different decision about where in their pipeline to put a validation gate —
and should come away unable to conclude that adopting Moon Dev's approach means adopting
Moon Dev's trades.

Related: [AI agent architecture](ai_agent_architecture.md) ·
[Controlled experiment design](controlled_experiment_design.md) ·
[Historical corpus](historical_corpus.md)

Related knowledge: [hypothesis discovery loop](../research_integrity/hypothesis_discovery_loop.md) ·
[research integrity checklist](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) ·
[hypothesis schema](../methods/HYPOTHESIS_SCHEMA.md) ·
[failure modes](../methods/failure_modes.md) ·
[methodology references](../frameworks/methodology_references.md)

## The central claim: a strategy is an argument, not an artefact

The repository's framing is that a strategy must originate from a **repeatable trading
decision**, decomposed before any code exists. The unit of work is not a bot. It is an
argument with five parts, and an argument missing any part is not ready to be coded:

| Component | Question it answers | Fails when |
|---|---|---|
| Hypothesis | What do I believe about market behaviour? | The statement is unfalsifiable or purely tautological |
| Mechanism | *Why* would that belief produce a tradeable effect? | The mechanism is a label ("mean reversion works") with no causal story |
| Observable variables | Which quantities would I have to be able to compute? | The variables do not exist in the data, or exist only after the decision |
| Formal rules | Exactly what is computed, when, and on what cadence? | The description contains discretion ("buy when it looks strong") |
| Testable prediction | What observation would refute this? | No observation can refute it |

The critical structural point: **the ordering is enforced, not merely recommended**.
Implementation is the last stage of a claim that was already fully specified. A bot built
without a prior hypothesis cannot be evaluated, because there is no prediction to check.

### Why this is the load-bearing idea for us

An LLM asked to "come up with a trading strategy" will produce plausible-sounding rules at
near-zero cost, and there is no natural constraint stopping it. The five-part decomposition
is that constraint, expressed in a form an LLM can be made to satisfy. It converts an
open-ended generation task into a completion task with checkable slots — which is exactly
what our [formalized RuleSpec](../../layer1/contracts.py) encodes: `hypothesis.statement`,
`hypothesis.null`, `expected_mechanism`, `features`, `signal.condition`, `signal.horizon`.

## The five research principles

1. **research_before_implementation** — no code is written for a strategy until the
   research question and its falsifier exist in prose. The purpose is to force the
   author to discover, before any engineering cost is sunk, that the effect is not
   observable in the data they have.
2. **hypothesis_before_code** — the hypothesis is an artefact with its own identity and
   lifecycle, not a comment at the top of a script. It can be rejected, superseded, and
   versioned while remaining separable from any implementation of it.
3. **backtest_before_live** — the strategy meets data it has never seen before it meets
   capital. The backtest is the first and only legitimate test of the hypothesis.
4. **separate_research_from_execution** — the artefact produced by research is a
   specification; the artefact produced by implementation is a process that consumes
   specifications. They do not share a process, a codebase, or a run loop.
5. **preserve_experiment_provenance** — every experiment records what it ran on and with:
   data version, feature set, rule version, model, prompt. Without this, a result is
   unrepeatable, and an unrepeatable result cannot be distinguished from a coincidence.

## What this changes about our agent design

The loop is a set of **gates between stages, each with a durable artefact**, and each gate
is where a machine can fail closed:

- **Research before implementation** means the mechanism-extraction step must produce a
  *documented economic mechanism* before any hypothesis is drafted. Our `research_notes`
  table is the durable home of that mechanism and the hypothesis row carries a foreign key
  to it ([`db/schema.sql`](../../db/schema.sql)). A hypothesis whose mechanism is empty or
  generic is structurally impossible — the column is `NOT NULL` and the critic has a
  `mechanism_plausible` dimension to rule on.
- **Hypothesis before code** means the pipeline order is sources → mechanisms →
  hypotheses → critique → formalized spec → queue → Layer 2. Nothing in our layer
  synthesises a rule before its hypothesis exists, and nothing in Layer 1 executes
  anything at all.
- **Backtest before live** maps onto our boundary: Layer 1 has no orders, positions,
  fills, balances, or exchange credentials in its schema. Layer 1 cannot trade because
  the database it owns has nowhere to put a trade.
- **Separate research from execution** is a *process* boundary, not just a schema one.
  Our queue is the entire interface: `experiment_queue` is written by Layer 1 and read by
  Layer 2, with identity columns frozen by trigger so Layer 2 cannot quietly edit the
  claim it is testing.
- **Preserve provenance** is why `research_identity` exists and why `rule_version`,
  `data_version`, `model_used`, and `prompt_version` are mandatory columns rather than
  optional metadata. It is also the reason we store `spec_sha256` and make
  `hypothesis_versions` append-only.

### Research-before-code changes *where the LLM is allowed to be creative*

Free-form creativity and rigour are inversely related in a research layer. If the model is
invited to "find something that works", its output distribution is driven by plausibility of
prose, not by falsifiability. If it is instead invited to fill a structured record derived
from an already-extracted mechanism, its creativity is confined to mechanism elaboration
and rule formalisation. This is why our domain universes are bounded
([`layer1/domains.py`](../../layer1/domains.py)): the space of things the extractor may
propose is a closed set, so proposals can be *compared* rather than merely enumerated.

The second consequence: the critic must be hostile and must be able to write a failure
record. `failures` is a first-class table with `stage IN ('critic','layer2_backtest')`.
A pipeline that can only produce candidates has no research loop, only a search loop.

## The anti-pattern rule

> **Never transform "Moon Dev used X" into "X works."**

Moon Dev's repositories are evidence about *how he reasons and builds*. They are not
evidence about *whether his reasoning produces tradable results*. The second claim is
unsupported by the first, and adopting the first gives no license whatsoever to adopt the
second.

The failure mode this prevents is subtle and worth naming: a research corpus of publicly
documented strategies is highly tempting as a source of "prior art to build on". But
"prior art" in the sense of *this is the thing to test next* and prior art in the sense of
*this has been shown to work* are different statements with different evidentiary
requirements. Only the first is supported here. Where this corpus is used, it is used as
a [historical corpus](historical_corpus.md) — a record of what has been tried and with
what assumptions — never as a source of validated results.

Concretely, in code: no mechanism extraction may cite "used by Moon Dev" as part of
`mechanism_description`, no hypothesis may be justified by the popularity of a public
strategy, and the critic must treat any such claim as *not evidence for the hypothesis*.

## What we deliberately did not copy, and why

- **The loop shape as a human workflow.** We kept the gates but removed the human from the
  loop's interior: research, critique and formalisation all run unattended. The gates
  survive; the manual steps do not.
- **Any reliance on a single author's judgement as a quality filter.** We replace it with
  an explicit critic rubric (mechanism plausibility, measurability, falsifiability,
  distinctness, no hidden discretion, no future leakage, no prior-failure collision) so
  that the quality filter is inspectable rather than a matter of taste.
- **Outcome targets attached to the research stage.** Moon Dev's own implementation pairs
  the loop with constants such as `TARGET_RETURN = 50` and `SAVE_IF_OVER_RETURN = 1%`.
  We rejected this outright; the reasoning is set out in full in
  [AI agent architecture](ai_agent_architecture.md). A research layer that can see a
  profitability target becomes a backtest-gaming machine.
- **Markdown/project layout as the store of record.** Their loop lives in files. Our
  loop lives in an append-only database, because a hypothesis that only exists as a file
  has no stable identity, no version history, and cannot be queried against what came
  before it.

## Epistemic status

**Primary source for Moon Dev's methodology. Not a source of evidence for any trading
result.** The methodology described here was read from the repositories themselves
(README-level claims, project structure, and code-level constants such as the research /
backtest / implement stage names). The figures that motivate our
[controlled experiment design](controlled_experiment_design.md) document — a survey of 77
studies with 19 primary, 2 of 19 using time-consistent splits, 1 of 19 with a
transaction-cost model, 1 of 19 documenting survivorship, 11 of 19 reporting execution
timing — come from the user's supplied research notes, not from our own reading; treat
them as user-supplied until independently verified.

A future agent should treat this file as normative: it tells you what our pipeline must
enforce. It must never be cited as support for a hypothesis's expected behaviour in the
market. If a hypothesis needs evidence, it needs a source in `sources` with an assigned
tier ([`db/schema.sql`](../../db/schema.sql)) — and this document is not one.