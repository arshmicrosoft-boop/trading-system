# Hypothesis Discovery Loop

## Purpose

This document extracts the *generation architecture* from the 2026 literature on agentic
financial research — principally Moon Dev's Research -> Backtest -> Implement framing and the
agentic loop studied in **"Can AI Do Financial Research?" (SSRN 6569258)** — and converts it
into a lineage contract for Layer 1.

The single most important correction the literature forces on us: the unit of research is
**not a hypothesis**. It is a *family* of hypotheses connected by recorded mutations. A
generator that emits a flat list of independent ideas produces combinatorially many shallow,
mutually-inconsistent, unattributable strings. What makes agentic research auditable is the
**generational memory**: the corpus remembers which idea was revised, into what, and why.

The loop described in SSRN 6569258 is `propose -> critique -> test -> revise -> new hypothesis`,
with approximately **1,600 hypotheses evaluated per run** across **three primary runs**. That
volume is the point: at 1,600 evaluations per run, no human reads the output. Selection,
lineage and rejection have to be structural, not attentional.

**What this source is NOT evidence of.** It is not evidence that the loop *produces profitable
strategies*, and the ~1,600 figure is a measure of **search throughput**, not of any trading
performance. We deliberately do not record, target, or optimise toward a return objective; see
the no-target rule in [RESEARCH_INTEGRITY_CHECKLIST.md](RESEARCH_INTEGRITY_CHECKLIST.md).

## Key principles extracted

### 1. The loop is a control system with a feedback edge, not a generation call

Five named stages, and each stage has a different owner in our two-layer split:

| Stage | Owner | Artifact produced | What Layer 1 must guarantee |
|---|---|---|---|
| propose | Layer 1 generator | candidate hypothesis | mechanism stated before any number is seen |
| critique | Layer 1 Research Critic / Integrity Checker | `Critique` with PASS/FAIL + `failure_type` | a FAIL must name a dimension, never a vague rejection |
| test | **Layer 2** | completed experiment result | Layer 1 is structurally unable to run a backtest |
| revise | Layer 1 generator, **conditioned on the critique and the test result** | child hypothesis with a declared mutation type | the mutation type and revision reason are mandatory, not optional |
| new hypothesis | Layer 1 generator, seeded from *failed or informative* branches | new family root | new branches are justified by what the previous generation failed to do |

The feedback edge is the whole design. Generation 2 is not "call the model again with more
ideas" — it is generation 2 **conditioned on the failures of generation 1**. The mutational
pressure comes from `failures` rows, not from resampling randomness.

### 2. Generational memory, not idea dumping

The naive reading — "an LLM generates 100 random trading ideas, we keep the promising ones" —
is precisely the failure mode the loop replaces. It has three defects:

- **No attribution.** Without lineage, a later success cannot be traced to the mechanism that
  caused it, so knowledge does not accumulate; only the surviving strings do.
- **No convergence signal.** Random generation explores uniformly. The loop concentrates
  effort on branches where a test actually returned information (including a clean refutation).
- **Redundant search.** Independent generations re-derive the same idea with different words,
  burning budget and inflating the apparent size of the corpus with duplicates.

Under generational memory, the unit stored is `(generation_id, parent_hypothesis_id,
mutation_type, revision_reason)`. A hypothesis with no recorded ancestry is not a research
artifact; it is a guess.

### 3. The mutation taxonomy for revisions

Every revision of an existing hypothesis must declare exactly one mutation type. The type
determines what Layer 2 is being asked, and it constrains which failure explanations remain
live.

| Mutation type | Meaning | When to use it | What it must change |
|---|---|---|---|
| **NEW** | A first-generation hypothesis with no parent; a new research branch. | A mechanism was extracted from a source with no prior child hypothesis. | `parent_hypothesis_id = null`, `generation_id = 1`. |
| **REFINEMENT** | Same mechanism, tighter or corrected specification — a threshold, a window, a conditioning qualifier. | A test came back ambiguous, or the statement turned out to admit more than one implementation. | The mechanism is unchanged and still believed; only its precision changed. |
| **GENERALIZATION** | The parent held in one setting; the child drops a restriction (different asset class, different horizon, continuous instead of binary predictor). | A test failed only in one universe or horizon and the mechanism is plausible elsewhere. | The claim's scope widens. Risk: generality was never tested; the child inherits the parent's untested assumption. |
| **SPECIALIZATION** | The child restricts the parent to a subset where the mechanism is strongest. | The parent's result was inconclusive in aggregate, but regimes were identifiable where the mechanism should carry most of the signal. | The child makes a narrower, sharper, more falsifiable claim. |
| **COMBINATION** | Two hypotheses from different mechanisms are tested jointly; the child is their conjunction or interaction. | Two mechanisms plausibly drive the same return and may not be separable observationally. | Both `parent_hypothesis_id`s must be recorded. Must state which factor is claimed to be the operative one — see the attribution rule in [knowledge_leakage.md](knowledge_leakage.md). |
| **CONTRADICTION** | The child is a deliberate mutual exclusion: it asserts the parent's predicted relationship does **not** hold, or holds with opposite sign. | A test result contradicts the parent and we want the contradiction tested rather than argued away. | The child must name the parent and the exact axis of disagreement. This is the only legitimate way to disagree with your own prior. |
| **REPLICATION** | The child re-tests an existing hypothesis with the same specification on different data, universe, venue, or period. | External corroboration is needed; or a parent was `rejected` on protocol rather than on substance and deserves a clean re-test. | No specification change at all. Any change of substance makes it a REFINEMENT or a different type — a REPLICATION with a changed rule is not a replication. |

Two rules make the taxonomy load-bearing rather than decorative:

- **A revision that cannot be typed is not a revision.** If a proposed child changes the
  mechanism, the horizon and the universe at once, it is a NEW hypothesis wearing a parent's
  name. Lineage contamination is worse than an honest orphan.
- **SPECIALIZATION is not free.** Every specialization doubles the number of things that must
  be tracked, and a specialization that is never tested leaves the parent's failure unexplained.
  Specializations must still pass the full integrity checklist.

### 4. The database consequence

The corpus must be an **evolutionary research tree**, not a flat pile of strategies. For every
hypothesis we need, retrievable as one query:

- `parent_hypothesis_id` — which hypothesis this revises (two parents for COMBINATION);
- `generation_id` — depth in the tree, root = 1;
- `mutation_type` — one of the seven values above;
- `revision_reason` — the evidence or critique that motivated this exact change, with a pointer
  to the `failure_id` or `Critique` when there was one.

**Implementation note (no schema change required).** `hypotheses` and
`hypothesis_versions` currently carry no lineage columns, and Layer 1 does not alter
`db/schema.sql`. The lineage block therefore travels inside the formalized spec, which is
already JSONB, already immutable, and already content-addressed by `spec_sha256`:

```yaml
lineage:
  generation_id: 3
  mutation_type: REFINEMENT
  revision_reason: "critique C-118: horizon ambiguous between 4h and next-session-open"
  evidence_refs:
    - failure_id: 41
    - critique_id: C-118
  parents:
    - hypothesis_id: H-0042
```

Because `hypothesis_versions` is append-only, this lineage record is tamper-evident: editing a
revision's declared reason requires inserting a new version, which leaves the original visible.
A dedicated lineage table is the cleaner long-term home, but Layer 1 can enforce and query the
tree today through this block. The Research Critic must treat a missing or untyped
`lineage` block as a formalization failure, not as an omission to be backfilled.

### 5. The query this makes possible

With lineage stored, one question becomes answerable, and it is a **research finding in its own
right**:

> How many times has this research family been revised, by which mutation types, and how did it
> fail out of sample each time?

"Refined 7 times and still fails out of sample" is not a null result — it is positive evidence
that the mechanism is wrong, or that the specification has been fitted to noise seven times
over. It converts an expensive search into a short conclusion, and it should terminate the
branch and raise a mechanism-level CONTRADICTION rather than triggering an eighth refinement.

This is also the honest answer to "the generator produced 1,600 hypotheses": most of them are
nodes in a tree, most trees terminate, and the value of the corpus is measured by how quickly
it kills bad mechanisms — not by how many strings it stores.

## Direct consequences for Layer 1's architecture

1. `hypotheses.expected_mechanism` stays mandatory: a mutation is only typable if the mechanism
   is stated, and "GENERALIZATION" is meaningless without the scope it widens.
2. The Research Critic's novelty question is answered by **the tree, not by the string**:
   novelty is low when a proposed child is reachable from an existing node by a declared
   mutation, and high when the candidate variables sit in a different domain universe.
3. Duplicate detection must run against *all generations*, not just the current batch — a
   REPLICATION or REFINEMENT duplicate is a lineage error, not merely a corpus duplicate.
4. Generation is bounded by evidence: the prompt for generation N+1 receives the failure
   reasons from generation N. Without that conditioning the loop degenerates into the idea dump
   it was designed to replace.
5. The tree is what Layer 2 receives. A queued experiment carries a `version_id`; the chain of
   `version_id`s behind it is the audit answer to "why was this rule chosen".

## What we deliberately did NOT copy, and why

- **We did not adopt any profitability objective for the loop.** A selection pressure toward
  profit makes the generator propose hypotheses that flatter whatever scoring is in use; it
  selects for overfitting, not for truth. Generation pressure comes from mechanism quality and
  falsifiability.
- **We did not copy the ~1,600-hypotheses-per-run scale.** Throughput is a property of the
  cited study's budget, not a design target. What we copy is the *shape* of the loop and its
  lineage obligations; throughput is bounded by our own review capacity.
- **We did not let Layer 1 execute the `test` stage.** Moon Dev's RBI runs the backtest inside
  the loop. Splitting it out is what lets us keep the research layer honest: the layer that
  writes hypotheses is not the layer that observes returns.
- **We did not make the loop's memory a natural-language transcript.** Transcripts do not
  survive query. Lineage is structured fields, because the query in section 5 has to be
  executable.

## Epistemic status

The propose -> critique -> test -> revise -> new-hypothesis loop, the ~1,600-hypotheses-per-run
scale, and the three primary runs are **user-supplied summaries of SSRN 6569258**, not
verified by us against the paper text. The mutation taxonomy is **our synthesis**: the seven
types are standard in evolutionary-computation and automated-program-repair literature, and
they are mapped here onto research-hypothesis revision. Treat the taxonomy as a design
decision of this project, revisable by the Research Critic, and treat the SSRN figures as
second-hand. No claim in this document is evidence that any of it works — the loop is a
proposal to be falsified like any other hypothesis in the corpus.

Related: [RESEARCH_INTEGRITY_CHECKLIST.md](RESEARCH_INTEGRITY_CHECKLIST.md),
[reproducibility_audit.md](reproducibility_audit.md),
[knowledge_leakage.md](knowledge_leakage.md),
[../moondev/rbi_methodology.md](../moondev/rbi_methodology.md).