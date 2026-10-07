# Reproducibility Audit

## Purpose

The agentic-trading literature is, at the time of writing, **measurably unable to reproduce its
own results**. This document establishes that as the motivating problem for Layer 1's
Research Integrity Checker, and defines the protocol contract that makes a hypothesis either
auditable or unqueueable.

The 2026 **Agentic Trading survey (arXiv 2605.19337)** audited **77 LLM trading-agent
studies**. **19 met primary empirical criteria.** Of those 19:

- only **2 of 19** reported extractable, time-consistent split protocols;
- only **1 of 19** reported an explicit transaction-cost model;
- only **1 of 19** documented universe / survivorship handling;
- **11 of 19** reported execution timing / semantics;
- **none** reached the survey's highest reproducibility level.

Read those five numbers as a funnel. Of the primary empirical work in this field, roughly one
study in ten could even be split in time consistently, and roughly one in twenty could be
re-run with costs. The overwhelming majority of published claims in agentic trading are
**not falsifiable by a third party**, because the reader is not told how the clock was set,
what the spread was, or which instruments were alive.

**What these figures are and are not.** They are a measure of *reporting discipline*, not of
whether the underlying strategies worked. A study with no cost model does not have bad returns;
it has **unmeasurable** returns. The survey's findings are used here solely as an argument for
protocol rigour. They are never a performance benchmark, and Layer 1 sets no target derived
from them.

## Key principles extracted

### 1. An unspecified protocol is the single largest source of irreproducible claims

The funnel above is not a list of random omissions. All five numbers describe the same defect
from different angles: **the reader cannot reconstruct the experiment**. A hypothesis whose
protocol is incomplete is not a hypothesis that is "probably fine". It is a claim that has
lost the property that made it a claim.

This drives the hard rule below.

### 2. The hard rule: INCOMPLETE is a state, not a judgement

> **A hypothesis whose experimental protocol is incomplete has
> `RESEARCH_STATUS = INCOMPLETE`. It is never `READY_FOR_BACKTEST`.**

This is deliberately a **state distinction rather than a judgement call**, because the two get
conflated in practice and the conflation is the bug:

- **INCOMPLETE** says the hypothesis has not yet earned the right to consume a backtest. It is
  not refuted. It is not weak. It is *unfinished research*, and it is recoverable: fill the
  protocol, re-enter the Integrity Checker, and it may pass.
- **FAIL** says the hypothesis is bad — implausible mechanism, unmeasurable, not falsifiable. It
  is recorded in `failures` so the family does not re-propose it.

Collapsing them produces two symmetric errors. Treating INCOMPLETE as FAIL discards good
mechanisms for clerical reasons, and trains the generator to hide its own gaps. Treating
INCOMPLETE as PASS is far worse: it is how a hypothesis with no cost model reaches a backtest
and reports a number that nobody can reproduce. **The default when a required protocol field is
missing or null is INCOMPLETE, and INCOMPLETE is terminal for this attempt at queueing.**

Mapping onto the current schema, without altering it: INCOMPLETE means the hypothesis remains
at `critiqued` — non-terminal — and **never** reaches `formalized` or `queued`. A `rejected`
status is reserved for an actual FAIL verdict.

### 3. Why the database already enforces this

The reproducibility contract is not a wish. `research_identity.data_version` is `TEXT NOT
NULL`, and `experiment_queue.research_id` is a `NOT NULL UNIQUE` foreign key into it. A
hypothesis with an unspecified dataset has no `research_id`, and therefore **cannot physically
obtain a row in `experiment_queue`** — the gap is caught by Postgres, not by reviewer
discipline. The Integrity Checker's job is to detect the incompleteness *early and legibly*,
with a named failure reason, instead of letting the INSERT fail as an opaque constraint
violation at the end of a long pipeline.

## The protocol contract

Formalization is only complete when all five blocks are present and concrete. "Concrete" means
an identifier, a number or an enumerated policy — not a description of one.

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
  timestamped_at:  "YYYY-MM-DDTHH:MM:SSZ"   # when this spec was pinned to a snapshot
```

### Block-by-block rationale, mapped to the survey

| Block | Survey evidence it answers | What a null value actually means |
|---|---|---|
| `temporal_protocol` | 2/19 reported extractable time-consistent splits | The evaluation is uninterpretable: with an arbitrary split, every result is available by choosing dates. |
| `universe` | 1/19 documented universe / survivorship handling | The return may come entirely from assets that were dead or delisted. The instrument list is a hidden degree of freedom. |
| `execution` | 11/19 reported execution timing / semantics | The best case in the funnel, and still a minority. Filling bars and firing at the close of the same bar is look-ahead by construction. |
| `costs` | 1/19 reported an explicit transaction-cost model | Without a spread and slippage assumption, gross and net are the same number and the claim is unfalsifiable. |
| `data` | required for `research_identity` | The result cannot be re-derived later; "latest" is not a dataset. |

The three worst-covered blocks in the survey — costs, universe, splits — are exactly the three
that require the *author* to commit to a choice they would rather leave open. That is the
structural bias the Integrity Checker exists to counteract: **an omitted protocol field is a
free parameter, and free parameters are where published claims go to die.**

## Direct consequences for Layer 1's architecture

1. **Layer 1 does not own data or datasets.** It does not download bars, choose a provider, or
   resolve a `dataset_version`. Its responsibility is narrower and checkable: record that these
   fields are **UNDEFINED**, and refuse to queue the hypothesis until they are specified. The
   value of a `null` here is that it is loud and early.
2. **The Integrity Checker is a blocking gate, not a report.** Its output is
   `READY_FOR_BACKTEST` or a named non-ready state. There is no "PASS with caveats" path, and
   no threshold at which enough fields are populated — the contract is all-or-nothing, because
   a partially specified protocol has no principled degree of completion.
3. **UNDEFINED is a first-class stored value, not `NULL` and not empty string.** The absence
   must be legible to a query ("how many queued experiments had an undefined cost model?" —
   answer must be zero, forever) and to a human reading the row.
4. **Cost blocks are mandatory for perpetual futures.** A `forced_flows` hypothesis about
   liquidations or funding on a perpetual venue without `costs.funding: applied` is INCOMPLETE
   by rule.
5. **Protocol completeness is checked before the hypothesis is ever formalized into a version**,
   so an incomplete hypothesis costs a critique, not a schema round-trip.

## What we deliberately did NOT copy, and why

- **We did not adopt a reproducibility score, grade, or threshold.** "80% reproducible" is not
  a state a machine can act on. Completeness of the five blocks is binary and therefore
  enforceable.
- **We did not accept "we will document it in the appendix".** Appending detail after the
  result exists is precisely the pattern the survey's funnel exposes.
- **We did not define a default spread, slippage, or fee schedule.** A default is a free
  parameter in disguise: it lets the author skip a choice while still consuming it. Layer 1
  marks these UNDEFINED and escalates the choice to whoever owns the data.
- **We did not reproduce the survey's ranking or tiering of studies as a quality score for our
  own hypotheses.** We copy the *failure taxonomy*, not the standings. Our hypotheses are not
  competing with published ones.
- **We did not make Layer 1 responsible for data quality.** Layer 1 records the pointer; it
  does not validate a vendor's fills.

## Epistemic status

Every figure in this document — **77 studies audited, 19 meeting primary empirical criteria, 2
of 19 with extractable time-consistent splits, 1 of 19 with an explicit transaction-cost
model, 1 of 19 documenting universe/survivorship handling, 11 of 19 reporting execution
timing/semantics, none reaching the survey's highest reproducibility level** — is
**user-supplied from arXiv 2605.19337** and has not been verified against the survey text by
us. We have not seen the survey's coding rubric, and "primary empirical criteria" is used here
as the user conveyed it. A future agent should treat these numbers as the reason this component
exists and as *secondary* reporting about other people's papers — never as measurements of any
strategy, ours included. If the survey is later read directly, correct these figures here
first.

The five-block YAML contract is **our own design**, not a reproduction of any published
protocol standard; it is a minimal union of the omissions the survey found, and is expected to
evolve with Layer 2's requirements.

Related: [RESEARCH_INTEGRITY_CHECKLIST.md](RESEARCH_INTEGRITY_CHECKLIST.md),
[knowledge_leakage.md](knowledge_leakage.md),
[hypothesis_discovery_loop.md](hypothesis_discovery_loop.md),
[../moondev/controlled_experiment_design.md](../moondev/controlled_experiment_design.md).