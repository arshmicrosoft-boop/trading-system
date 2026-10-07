# REPLICATION

Replication as a first-class hypothesis type. Not an afterthought, not a fallback when novel generation runs dry.

Related: [contradiction_resolution.md](contradiction_resolution.md) (a contested claim is the best replication target there is) · [failure_modes.md](failure_modes.md) · [hypothesis_testing.md](hypothesis_testing.md) · [HYPOTHESIS_SCHEMA.md](HYPOTHESIS_SCHEMA.md) · [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md) (the mutation taxonomy — `REPLICATION` is one of its seven types) · [../research_integrity/reproducibility_audit.md](../research_integrity/reproducibility_audit.md) · [../research_integrity/knowledge_leakage.md](../research_integrity/knowledge_leakage.md) · [../frameworks/methodology_references.md](../frameworks/methodology_references.md)

## The problem replication solves

A large fraction of what passes for evidence in this field is not evidence. It is "a paper claims X" — a claim with an unexamined chain between the reported result and our conditions of use. We did not run it, we did not check its cost model, we do not know its sample, and the paper's authors were optimising for publication rather than for our question.

That is a serious problem for a system whose entire value is knowing which claims survive contact with data. An unexamined external claim imported as a hypothesis inherits every unexamined assumption of its source, and then gets tested as if it were our own careful reasoning. It is the mechanism by which a corpus launders a decade of questionable practice into apparently-tested belief.

The user-supplied survey figures — **from the user's research notes, not from our own reading** — give the scale: of 77 studies surveyed, 19 were primary work; only 2 of those 19 used time-consistent splits; 1 of 19 had a transaction-cost model; 1 of 19 documented survivorship; 11 of 19 reported execution timing. Roughly one in nineteen is comparable to anything we would run. Treat those counts as directionally decisive and treat any individual number as unverified until the source is in hand.

Replication makes the chain explicit. Instead of importing a conclusion, we import a **question**: does this reported relationship survive under *our* data and *our* execution assumptions?

## The shape

A replication hypothesis is a hypothesis with a different provenance requirement. Novel generation asks "what might be true?"; replication asks "is this specific reported thing true here?"

```
type:            replication
source_claim:    <the claim under test, verbatim, with source_id and tier>
question:        does the reported relationship survive under our data
                 and our execution assumptions?
divergence:      what specifically differs between their setup and ours
                 (cost model / horizon / sampling frequency / universe /
                  venue / market structure / data quality)
falsification:   the observation that would count as a failed replication
```

The `divergence` field is what makes this worth doing rather than ceremonial. A replication that changes nothing from the source tells us only what the source said. A replication that changes the cost model, or the sampling frequency, or the universe, produces a **measurement of the gap** — and the gap is the finding. It also naturally produces a conditional hypothesis, which is the same move as [contradiction_resolution.md](contradiction_resolution.md) arrived at from the other direction.

Filled out as a Layer 1 artifact:

> **Hypothesis (replication).** The reported relationship between [variable] and [target] holds in gross form but not after a realistic cost model, on our venue, at our sampling frequency.
>
> - `source_claim`: source `S`, tier `T`, asserts [verbatim claim]
> - `mechanism`: [why the relationship would exist, and what would limit it]
> - `prediction`: net predictive power is materially smaller than the reported gross figure; sign is preserved
> - `direction`: conditional
> - `null`: net predictive power is indistinguishable from the reported gross figure under our costs
> - `alternative_explanations`: the gap is a data-quality artefact; or the reported figure used a different variable construction
> - `expected_failure_conditions`: the relationship survives costs intact, meaning the source's gross figure was conservative and their execution assumptions were pessimistic

Note what is being asked. Not "can we make money from this" — never, at this layer. Not even "is this effect large." The question is whether a specific reported relationship survives under stated conditions. That is answerable, and it has two possible answers.

## Why replication is safer than novel generation

The comparison is not close, and it should drive generation policy.

**Novel generation invents the claim and the test together.** A model asked for a new indicator combination chooses a variable, chooses a pairing, chooses a horizon, and states a prediction — then, without noticing, chooses the test that fits. The result is usually unfalsifiable, because nothing in the process constrained it. This is exactly the trap documented in `layer1/domains.py`: free-association over every indicator ever named produces combinatorially many shallow hypotheses that nobody can falsify.

**Replication imports an external anchor.** The claim was fixed before we touched it, by someone with no stake in our pipeline, and the test is determined by our stated execution assumptions rather than chosen to fit. The generator's degrees of freedom drop from "invent anything" to "state the conditions under which an existing claim would fail." That is a much smaller space, and most of it is occupied by legitimate hypotheses.

This is also the counter to `not_falsifiable`. A replication's `null` is not a matter of opinion — it is "the effect did not survive our costs" or "it did," and both are informative. Novel generation rarely achieves that clarity, because the claim was authored to be true.

## The cost/benefit

| | replication | novel generation |
|---|---|---|
| cost to evaluate | low — the claim and the test are both already specified | high — the mechanism may need to be built, then refuted, then rebuilt |
| probability of a well-formed artifact | high — external anchor constrains both sides | low — most output is indicator soup or post-hoc |
| outcome definiteness | definite — it survives or it does not | usually indefinite — "partially", "under the right conditions" |
| knowledge produced | a measured boundary between two sets of conditions | a new question, sometimes |
| corpus pollution on failure | low — the failure is attributed to a named external claim | high — an unfalsifiable sibling is now permanently in the corpus |

Three things follow.

**Replication is cheaper.** The expensive part of novel generation is deciding what to build. Replication arrives with the claim specified, the variables named, and the divergence enumerable. Layer 2 can evaluate it without further research-engine work.

**Replication produces definite knowledge either way.** A successful replication extends an external claim to our conditions — that is real, recorded, reusable. A failed replication is arguably more valuable: it locates the boundary of a published result, which is a finding nothing else in this system produces. Given how few published studies model costs and time-consistent splits, the base rate of informative failures is high.

**Novel generation mostly produces noise.** Not always, and refinement over invention improves its hit rate — see [hypothesis_testing.md](hypothesis_testing.md). But its yield per unit of reviewer attention is lower, and its failure mode (an unfalsifiable artifact that can never be pruned) is permanent.

So: **prefer replication targets first when a claim is available; use novel generation where replication has no anchor.** A corollary for the generator: when `sources` contains a testable claim, the `REPLICATION` mutation should be preferred over `NEW`. The taxonomy in [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md) lists `REPLICATION` alongside `NEW`, `REFINEMENT`, `GENERALIZATION`, `SPECIALIZATION`, `COMBINATION` and `CONTRADICTION`; the ordering is ours, and replication ranks highest where an anchor exists.

## Replication is not a special case of anything

Two distinctions worth keeping sharp, because collapsing them is how replication gets misused.

**A replication is not a combination.** `COMBINATION` mutates by joining two existing claims into a new one. `REPLICATION` adds no new claim — it holds one fixed and varies the *conditions*. That is why a replication can succeed or fail definitively when a combination usually cannot: there is only one thing that could be true.

**A replication is not a contradiction.** [contradiction_resolution.md](contradiction_resolution.md) starts from two sources that disagree and produces a conditional hypothesis. Replication can start from one source and one set of divergent conditions, and can produce a binary answer. Where a source and a well-replicated result of ours disagree, that *is* a contradiction, and it should be recorded as one — with both claims attached and the reconciling conditions enumerated. Do not quietly prefer our own replication result over the published claim; both go in, and the conflict object carries both.

## Epistemic status

Our own operational method. The framing of replication as a distinct mutation type is a design decision, not a finding from the literature. The survey figures quoted above are **user-supplied** and unverified; they motivate the policy but should not be repeated as established statistics without retrieving the source.

A future agent should treat the *ordering preference* (replication before novel generation) as the load-bearing claim here. It is easy to regress — novel generation is more fun to watch, produces more artifacts, and looks busier. But busier is not better, and an artifact that cannot be refuted is a permanent liability in the corpus. The safety argument is narrow and should be preserved: **replication has an external anchor that constrains both the claim and its test; novel generation does not.**