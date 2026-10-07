# Knowledge Leakage

## Purpose

Two distinct leaks corrupt a research corpus, and they are usually confused with each other:

1. **Temporal look-ahead** — the hypothesis uses information that did not exist at the moment
   the decision was supposedly made. This is the leak everyone names, and it is the *less*
   dangerous of the two.
2. **Knowledge leakage** — the *reasoning agent itself* carries the outcome inside it. The
   model recognises the ticker, the date and the event from history and produces fluent,
   plausible, apparently independent analysis that is actually recall. The backtest then
   measures memory, not reasoning.

This document is the second one. It draws its primary evidence from **KTD-Fin (arXiv
2605.28359)**, whose reported mitigations are the practical template for our design: masking
identifiers and calendar information so historical narrative cannot leak into model reasoning,
and using **performance attribution rather than raw returns** to judge the output.

**What this is NOT evidence of.** It is not evidence that LLMs cannot reason about markets,
and it is not evidence that any specific result in our corpus is contaminated. It is a
structural hazard with a known fingerprint. Our job is to make the fingerprint visible in the
data model before the reasoning is used, not to conclude that outputs are worthless.

## Key principles extracted

### 1. The KTD-Fin failure mode: memorisation wearing the costume of analysis

When a model is asked to reason about a historical window it has almost certainly seen — a
major liquidation cascade, a funding squeeze, a famous regime break — the prompt does not need
to state the answer. The model's *recognition* of the event supplies it. Two failure
consequences follow:

- **Apparent intelligence from recognition.** The narrative "funding was extreme, then the
  market cascaded" is the correct narrative, and it can be produced with no reasoning at all.
  Every downstream artefact — the stated mechanism, the chosen variables, the predicted
  direction, the timing — is then *conditioned on the known outcome*, and none of it is
  independent evidence.
- **A backtest measures recall.** Replay that window and the model reproduces what happened,
  which looks like skill. The finding is about the model's memory, not about the mechanism, and
  the mechanism's record in the corpus is now wrong in a way nothing downstream can detect.

The contamination is **not visible in the output text**. Well-formed prose, correct
mechanism vocabulary and a confident directional call are all equally produced by recall and by
reasoning. That is precisely why the defence cannot be a text filter applied after generation:
it has to be applied to *what the model is allowed to see and what we store as its provenance*.

Mitigations we take from KTD-Fin, adopted structurally:

- **Mask identifiers and calendar information** in any task that elicits reasoning about a
  historical window. If the model cannot resolve *which* instrument *when*, it cannot recall
  the episode. Labels become anonymised panel identifiers; absolute timestamps become relative
  offsets from an undisclosed anchor.
- **Judge by performance attribution, not raw returns.** A return figure is the easiest thing
  in the world to produce from memory and the least informative about reasoning quality.
  Attribution asks *where* the return came from — which exposure, which period, which leg —
  and a recall-only system produces an attribution that does not match the claimed mechanism.

### 2. The four-way distinction our engine must never collapse

A research corpus becomes garbage in exactly one common way: a sentence's *epistemic type* is
lost between extraction, storage, and re-reading. Four types look identical in free text and
mean four different things:

| Type | Example | Status | What it can support |
|---|---|---|---|
| **FACT** | "BTC funding was positive." | Verifiable against data. | The state of the world at a time. Nothing predictive. |
| **CLAIM** | "Positive funding predicts reversal." | An assertion by a source or an author. | A hypothesis to be tested. Not evidence of anything. |
| **HYPOTHESIS** | "Extreme positive funding predicts negative next-4h return." | Layer 1's testable, falsifiable statement, with a threshold and a horizon. | A queue entry. Subject to the Integrity Checker. |
| **RESULT** | "The relationship did / did not survive testing." | Layer 2's observation about a specific test. | Evidence about that hypothesis, on that data, at that version. Never transferable. |

The failure mode is the silent slide **CLAIM -> FACT**: a source asserts a relationship, the
extractor stores it as a statement about the world, and later generations cite it as settled.
The second slide is **model_inference -> RESULT**: a model reasons that a relationship exists,
and because the sentence is grammatical, it is filed next to things that were measured.

### 3. Consequence: every extracted claim must be TYPED

Layer 1 therefore stores the epistemic type of every claim it extracts, and the type is part of
the claim's identity — it travels with the text, is filterable, and cannot be silently
upgraded. The four stored types:

- `fact` — asserted as a state of the world, sourced and checkable.
- `author_claim` — asserted by a named source or author about a relationship. Recorded with
  its source so a later reader can weigh who is claiming it.
- `empirical_result` — a measurement that was actually performed and reported.
- `model_inference` — produced by our own generator or critic. **Never** filed as any of the
  other three.

Two hard consequences:

1. **A model's inference can never masquerade as a source's finding.** `model_inference`
   claims are attributable to `model_used` and `prompt_version`, and when a hypothesis is
   generated the corpus records that its motivation was an inference rather than a citation.
   That distinction is the difference between "the literature says this" and "we thought of this",
   and it must survive to the reader of the queue row.
2. **Downgrading is allowed; upgrading is not.** A `claim` may be refined into a `hypothesis`
   by adding a threshold, a horizon and a direction. An `author_claim` cannot become a `fact`
   because a model found it plausible, and an `empirical_result` from a source cannot become
   *our* result because we agree with it.

### 4. The point-in-time rule

> A source available at time **T** may not use information that only became available after
> **T**.

Every source in the corpus carries:

- `publication_date` — when the source states it was published;
- `information_available_at` — the earliest time the *content* could have been known,
  including the lag before its publication (a December backtest read in February of the
  following year knows things a December trader did not);
- `future_information_risk` — an explicit flag, not an inference, set when the content
  describes realised outcomes without a method that would have excluded knowing them.

A source describing a realised event is a legitimate input to *mechanism extraction* — that is
how we learn that liquidations cluster. It is **not** a legitimate input to anything that
decides when to trade, and `future_information_risk` is what stops it silently becoming one.
Sources carrying the flag are usable for what the mechanism is; they are not evidence for when
to act.

### 5. Return attribution: positive return is not alpha

A realised positive return does not identify where it came from. The same number is consistent
with the proposed mechanism, with market beta, with a style or factor exposure, with a
favourable regime, with luck in a short sample, or with a survivorship artefact in the
universe (see the 1-in-19 universe reporting rate in
[reproducibility_audit.md](reproducibility_audit.md)).

Attribution is what distinguishes these, and attribution is only possible against a
**claimed mechanism**. You cannot ask "is this result consistent with the mechanism we
proposed?" if nobody recorded the mechanism at proposal time.

> **Consequence:** Layer 1 records the **claimed mechanism** on every hypothesis —
> `hypotheses.expected_mechanism` is `NOT NULL` for this reason — so that Layer 2, holding a
> realised result, can later test whether the result is *consistent with the mechanism that was
> claimed in advance*.

Attribution is the measurement KTD-Fin uses in place of raw returns for exactly this reason: a
raw number cannot distinguish reasoning from recall, while an attribution that fails to match
the claimed mechanism can.

## Direct consequences for Layer 1's architecture

1. Any generator task that reasons about a **historical window** is a contamination-risk task:
   identifiers and calendar anchors are masked, and the resulting claim is typed
   `model_inference` regardless of how confident it sounds.
2. `research_notes` extraction emits typed claims. A note with an untyped claim is a
   formalization failure, not a formatting nit.
3. `sources` carries the three point-in-time fields. A source with a null
   `information_available_at` is INCOMPLETE for any use beyond mechanism discovery.
4. Hypotheses generated from a `future_information_risk` source may be queued only if their
   stated mechanism does not depend on knowing the outcome, and the Integrity Checker asks
   this explicitly (see checklist item `CONTAMINATED_HISTORICAL_INFORMATION`).
5. `expected_mechanism` is the attribution key. It must be written as a *mechanism* — a causal
   path from a measured quantity to a price move — not as a restatement of the prediction.
   "Funding predicts reversal" is not a mechanism; "levered longs pay funding, forced
   unwinding sells, and that selling is the reversal" is.

## What we deliberately did NOT copy, and why

- **We did not adopt masking as a research convention across the whole system.** Masking
  identifiers destroys provenance. It is applied only to historical-window reasoning tasks, and
  the mask parameters are recorded so the run is auditable.
- **We did not treat "the model was right" as evidence the model reasoned.** Correct outputs
  are consistent with recall; that is the entire hazard. We do not build any credibility score
  from output correctness.
- **We did not import any published model, benchmark or dataset from KTD-Fin.** We copied the
  *mitigation shape* (mask identifiers/calendar; judge by attribution), not the artefact.
- **We did not add a "model confidence" field.** Confidence is the quantity recall inflates.
- **We did not forbid sources with realised outcomes.** Almost all mechanism knowledge comes
  from post-hoc studies; the flag governs how a source may be used, rather than whether it
  may be read.

## Epistemic status

**KTD-Fin (arXiv 2605.28359)** — the reported failure mode of historical information leaking
into model reasoning, the masking of identifiers and calendar information, and the use of
performance attribution instead of raw returns — is **user-supplied**, and the paper itself has
not been read by us. The paper's own empirical magnitudes, if any, are unknown to this document
and are not quoted. The four-way FACT / CLAIM / HYPOTHESIS / RESULT distinction is **our
design**, standard in evidence-typed research systems but not attributed to any single source
here. A future agent should treat KTD-Fin as **secondary, second-hand** reporting about a
mechanism we have independently plausible, and should read the paper before relying on its
specific mitigation details.

Related: [RESEARCH_INTEGRITY_CHECKLIST.md](RESEARCH_INTEGRITY_CHECKLIST.md),
[reproducibility_audit.md](reproducibility_audit.md),
[hypothesis_discovery_loop.md](hypothesis_discovery_loop.md),
[../methods/failure_modes.md](../methods/failure_modes.md).