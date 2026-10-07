# Hypothesis Testing

How a research idea becomes a claim that could be wrong, and how we rank the resulting claims for Layer 2 to spend compute on.

**This document contains no profitability target, and the score below is not a performance metric.** Read that twice before using the score, because it is the single easiest place in this corpus to accidentally smuggle one back in.

Related: [HYPOTHESIS_SCHEMA.md](HYPOTHESIS_SCHEMA.md) · [failure_modes.md](failure_modes.md) (the checklist this scoring assumes has already run) · [REPLICATION.md](REPLICATION.md) · [contradiction_resolution.md](contradiction_resolution.md) · [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md) (the mutation taxonomy) · [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md) · [../frameworks/tradingagents.md](../frameworks/tradingagents.md)

## Hypothesis versus strategy

A **hypothesis** is a claim about the world. A **strategy** is a claim about the world *plus* a decision rule plus an objective to optimise. The gap between them is where all the trouble lives, because a strategy will produce results whether or not its underlying claim is true, whereas a hypothesis cannot.

The distinction has five parts, and a Layer 1 artifact is a hypothesis only if it has all five:

| part | what it supplies | if missing |
|---|---|---|
| hypothesis | the claim | nothing to assess |
| mechanism | why the claim should be true | a coincidence awaiting explanation |
| observable variables | what would carry the mechanism | untestable as stated |
| formal rules | exactly how the variables are computed and acted on | irreproducible, hidden discretion |
| testable prediction | what must be seen, over what horizon | unfalsifiable |

The generator's failure mode is skipping straight from "hypothesis" to "rules", which produces a strategy wearing a hypothesis's clothes. The `Research Critic` detects this by asking whether the mechanism is load-bearing — if deleting the mechanism paragraph leaves the prediction unchanged, the mechanism was decoration and the artifact is a strategy. See [failure_modes.md §5](failure_modes.md) (indicator soup, undefined mechanism).

## The scientific minimum

Every hypothesis must answer five questions in prose, before any YAML is attempted. These are prompts for the generator, not database columns; they become fields on the way to becoming fields.

1. **What are we testing?** The claim, stated so that it is wrong in some possible world. If no world falsifies it, stop — it is a story.
2. **Why should the mechanism exist?** The economic or structural reason. "Because informed traders anticipate it" is a mechanism. "Because it has worked" is not.
3. **What exact variables represent it?** Names that resolve to something computable, ideally from a domain universe in `layer1/domains.py`. A variable that cannot be computed is `not_measurable` and ends the artifact.
4. **What observation would falsify it?** The single most important sentence. This becomes `HypothesisBlock.null`, and it must be a concrete observation at a concrete horizon — not "if it does not work".
5. **Where did the idea come from?** A `source_id`, or an explicit declaration that this is novel generation. An idea with no provenance and no novelty claim cannot be checked for duplication.

Note what is absent: nothing here asks how much the claim is worth. A hypothesis that answers all five perfectly and predicts nothing profitable is a perfectly good hypothesis. Whether it predicts anything profitable is Layer 2's question, asked with data, and only then.

## The research quality score

Eight dimensions, each scored 0-5, total 0-40. It exists to answer one question: **is this claim well-specified and well-motivated enough to deserve a Layer 2 experiment slot?**

| dimension | 0 | 5 |
|---|---|---|
| mechanism clarity | no account of why | full causal account that would apply to an asset the author has never seen |
| falsifiability | no possible observation refutes | `null` is concrete, timed and specific |
| evidence quality | hallucinated or absent | tier-1 peer-reviewed, directly on point |
| novelty | duplicate of something in the corpus | distinct from every stored hypothesis and every cited source |
| data availability | variables do not exist in our data | every variable is computable point-in-time from data we have |
| temporal integrity | look-ahead or undefined execution time | every feature value is knowable strictly before the decision timestamp |
| implementation clarity | parameters unstated or discretionary | every number is in the spec; two engineers would build the same thing |
| confounding risk | alternatives not considered, or indistinguishable | the main alternative explanations are enumerated and the design separates them |

Bands:

| total | band | action |
|---|---|---|
| 0-20 | reject | the artifact does not enter the queue; record the `failure_type` |
| 21-30 | weak | keep in the corpus, do not queue; prefer refinement over a fresh sibling |
| 31-35 | experiment | queue for Layer 2 at normal priority |
| 36-40 | high priority | queue first |

### What this score is not

**The research quality score measures research quality. It does not measure trading performance, and it is not a proxy for it, and it must never be used as one.**

It has nothing to do with whether a hypothesis makes money, might make money, or is likely to make money. No dimension above references a return, a risk-adjusted return, a hit rate, a direction of profitability, or a size. There is deliberately no "expected performance" dimension, because adding one would put a profit target back into the research layer through the back door — a generator optimising a composite score will learn to write dimensions that score well, and if one of them correlates with profitability it will write for that correlation instead of for truth.

Contrast with Moon Dev's own approach, which this engine rejects: his implementation carries `TARGET_RETURN = 50` with `SAVE_IF_OVER_RETURN = 1%`. A research layer that can see a return target will generate hypotheses optimised to flatter that target. It will report large effect sizes, plausible mechanisms, and no failure conditions, because those are what a return target rewards. See [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md) for the full divergence.

The two scores in this system are separate instruments and must not be merged:

- The **research quality score** (here, 8 dimensions × 0-5) ranks how well a claim is posed. Computed in Layer 1. Says nothing about money.
- The **critic's `Critique.scores`** (`layer1/contracts.py`, seven dimensions × 0-2) is the pass/fail checklist verdict. Computed in Layer 1. Says nothing about money.

When Layer 2 reports a backtest result, that is the first and only place in this system where trading performance appears — and it is evidence, not an input to how Layer 1 generates. Feeding it back as a generation objective is what closes the loop into reward hacking, catalogued in [failure_modes.md §4](failure_modes.md).

## Refinement over invention

When a weak hypothesis sits in the 21-30 band, the highest-value move is usually **not** to generate a fresh sibling. It is to sharpen the weak one.

Concretely, prefer, in order:

1. **Name the missing piece.** A weak hypothesis usually has exactly one soft dimension. Write the field that dimension measures.
2. **Narrow the claim.** Overbroad claims are weak because they are unfalsifiable in the over-broad regions. Adding a condition converts an untestable claim into two testable ones.
3. **Split the hedge.** "OFI predicts returns in some regimes" becomes two hypotheses with named conditions, each of which can lose.
4. **Enumerate the confound.** Often lifts confounding risk — the largest single source of weak scores — without new invention.
5. **Only then, generate a sibling.**

The reason is arithmetic and structural. Novel generation is unbounded: given the variable vocabulary in `layer1/domains.py` there are combinatorially many combinations, almost all of them indicator soup with no mechanism, and a system that generates freely produces a corpus it can never prune. Refinement is bounded by the artifact in front of you, keeps `related_hypotheses` dense, and improves the specific thing that was wrong.

This is also the `REFINEMENT` branch of the mutation taxonomy in [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md), and the `not_distinct` failure mode's natural remedy — a sharpened sibling is distinct; another fresh combination usually is not.

## Replication is a success

A hypothesis that reproduces a published result under our data and our execution assumptions is a **successful hypothesis**, not a consolation prize. It resolved. The question was asked, the answer was obtained, and the corpus gained definite knowledge.

This is worth stating because the pressure in an automated pipeline runs the other way: replication is slower to feel productive than a novel claim, and novel claims are what look like progress. But replication is cheaper to evaluate and produces definite knowledge, where novel generation mostly produces unfalsifiable noise. See [REPLICATION.md](REPLICATION.md).

The corollary matters for the queue: **a replication that fails is one of the highest-value records in the system.** It tells us the published result does not hold under our constraints — which, given how few published studies model costs, time-consistent splits and survivorship, is the single most likely thing we will learn. See [failure_modes.md §3](failure_modes.md) for the survey figures behind that claim and [../research_integrity/knowledge_leakage.md](../research_integrity/knowledge_leakage.md) for why an unexamined external claim is a hazard rather than evidence.

## What we deliberately did not copy

- **A return target with a save threshold.** Rejected outright; it converts the research layer into an optimiser of its own output. See above.
- **Free-form indicator generation across the full TA vocabulary.** `layer1/domains.py` exists specifically to bound the universe; the docstring records this as Moon Dev's mistake worth avoiding.
- **Automatic PASS on plausibility.** A well-written story is not a test. The critic is hostile by construction and a `FAIL` must name a `failure_type`; see `layer1/contracts.py` (`_coherent` validator).

## Epistemic status

This document is **our own operational method**, not a literature summary. The structural claims (hypothesis versus strategy, falsifiability as the load-bearing requirement, confounding) are standard scientific method and are not attributable to any single source here. The survey figures quoted by way of [failure_modes.md](failure_modes.md) are **user-supplied** and unverified. The Moon Dev constants are as reported in the user's notes about his implementation and should be re-checked against the source repository before being repeated to a third party.

A future agent should treat the score's *dimensions* as a prompt scaffold — they are deliberately shaped to keep profitability out, and editing one to include an expected effect size would reopen the door this document is built to close. Treat the *bands* as a starting point to be calibrated against real Layer 2 output, never as validated thresholds.