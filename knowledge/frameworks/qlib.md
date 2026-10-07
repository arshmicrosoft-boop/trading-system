# qlib — modularity as the reason our component boundaries exist

## Purpose

Microsoft's qlib is an infrastructure reference, not a strategy source. It contributes
exactly one thing we needed an argument for: **a quantitative research system is a
chain of loosely coupled stages, and the coupling must stay loose.** Every stage in
qlib owns its own data contract and can be swapped or tested without disturbing its
neighbours.

Critically, qlib is **not** evidence that any of its modelling results are correct, and
it is **not** a source of trading-performance expectations. It tells us how to *shape*
a system. It says nothing about whether a signal works. Importing a number from it into
a hypothesis would be a category error: infrastructure authorship confers no evidentiary
standing on the alpha it ships.

## The chain, and what "loosely coupled" means in it

qlib's research pipeline runs:

```
data -> alpha research -> modelling -> backtesting -> risk -> portfolio -> execution
```

Each arrow is an interface, not a function call chain. The alpha researcher does not
know how the backtester resamples; the risk module does not know whether the alpha came
from a human or a search process. Replace any stage and only that stage's tests move.

qlib's automated alpha work (the RD-Agent / AlphaGen line) is the same shape pointed
inward: the search loop generates candidate factor expressions in a restricted formal
grammar, evaluates them mechanically, and feeds results back. The loop is a stage, not
the centre of the system.

## THE PRINCIPLE: modularity over a single trading agent

The failure mode qlib is built to prevent is the "one giant trading agent": a single
prompt and a single model handed the whole problem, free to move from data cleaning to
signal generation to position sizing in one uninspectable reasoning trace. Nothing
about such a system can be unit-tested, and nothing about it can be held accountable
when it goes wrong, because there is no seam at which to intervene.

The alternative is a set of units with narrow interfaces. Our pipeline already *is*
that set. The tables exist because the stages need separate, independently addressable
records:

| Stage unit | Our persistent record | Interface it answers to |
| --- | --- | --- |
| Source | `sources` | a piece of immutable evidence, content-hashed |
| Mechanism | `research_notes` | a mechanism extracted *from a named source* |
| Hypothesis | `hypotheses` | a falsifiable claim *about a named mechanism* |
| Formalized spec | `hypothesis_versions` | a frozen, machine-evaluable expression |
| Provenance identity | `research_identity` | the tuple that makes a result reproducible |
| Experiment hand-off | `experiment_queue` | a row Layer 2 can claim without asking us anything |
| Rejection | `failures` | a negative result recorded as a first-class object |

These are the six draft tables plus `research_identity`, which the schema comment
records as added because the research_id contract needs columns none of the six held.
Each is separately addressable and separately testable. `layer1/components/__init__.py`
states the coding discipline in one line: the stages "share `layer1.contracts` for types
and `layer1.db` for access, and never import each other's internals."

## Direct consequences for Layer 1's architecture

1. **The table boundaries are architectural, not incidental.** A future contributor who
   finds `hypotheses` and `hypothesis_versions` "redundant" and collapses them destroys
   the ability to re-formalize a hypothesis without destroying the hypothesis — which
   is the whole point of versioning. The `UNIQUE (hypothesis_id, spec_sha256)` constraint
   is load-bearing for content-addressed identity, not a stylistic choice.
2. **Immutability is the seam that makes stages composable.** `sources` and
   `hypothesis_versions` are UPDATE-blocked by trigger; `experiment_queue` freezes its
   identity columns at enqueue. A downstream stage can trust that what it reads was not
   quietly revised by an upstream stage after the fact. That trust is what makes loose
   coupling safe.
3. **Failures are a module, not an exception path.** `failures` holds critic rejections
   and Layer 2 refutations in one table with a `stage` column. Rejection is a *result*
   with a typed `failure_type`; a component that cannot fail is a component that cannot
   be evaluated.
4. **The hand-off is a view, not an integration.** `experiment_queue_layer2` assembles
   the rule, its provenance and its hypothesis statement into exactly the row Layer 2
   needs. Layer 1 never writes a backtest result. The interface is a read contract, so
   Layer 2 can be rewritten wholesale.
5. **The bounded universes are the modularity discipline applied to hypothesis *space*.**
   `layer1/domains.py` defines microstructure, forced_flows, relative_value and
   price_volume as explicit domains rather than "every TA indicator ever named",
   precisely because an unbounded space yields combinatorially many shallow hypotheses
   that cannot falsify each other. The four domains, and `ALL_VARIABLES` as the
   computability vocabulary, are one unit's output consumed by the next.

### Formal languages for candidate signals (the RD-Agent lesson, correctly read)

The lesson from automated alpha discovery is **not** "let an LLM invent indicators".
Generating plausible-sounding indicators is the cheapest possible way to manufacture
hypotheses that cannot be evaluated — it produces text, not testable claims.

The actual lesson is narrower and much more useful: **represent candidate signals in a
formal language that can be mechanically evaluated.** If a candidate must be written in
a grammar, then it can be parsed, checked for computable variables, checked for
duplicate form, versioned by content hash, and handed to an evaluator without
translation loss.

This is precisely what our Rule Formalizer does. `RuleSpec` in `layer1/contracts.py`
is that formal language: a frozen, content-hashed, machine-evaluable expression with
`features`, a `signal.condition`, a `horizon`, an explicit `hypothesis.null` (the
observation that would refute the claim), and a `spec_sha256()`. A hypothesis that
cannot be written as a `RuleSpec` has not been made testable — it has merely been
made into prose. The `unformalizable` failure type exists to record that outcome.

Two supporting decisions follow: candidate variables are drawn from
`ALL_VARIABLES` so "is this computable from data we have?" is decidable in code; and
because the spec is content-addressed, re-formalizing identical content is a no-op
rather than a new claim.

## What we deliberately did NOT copy, and why

- **Not the execution and portfolio layers.** qlib's chain continues into risk,
  portfolio construction and execution. Layer 1 stops at `experiment_queue`. Our schema
  has no orders, positions, fills, balances or credentials, and that absence is the
  design, not an unfinished stage.
- **Not an alpha-expression search loop.** We do not run automated alpha generation. The
  research layer's job is to ask "is this well-defined and motivated enough to deserve
  testing?", and a search loop optimises in the wrong direction: it maximises the count
  of testable expressions, which is not the same as maximising the quality of research.
  Layer 2 and a deliberate human decide what is worth testing.
- **Not any performance claim qlib publishes.** Its backtest numbers are produced by
  qlib's own backtester, which is exactly the code whose assumptions we cannot audit
  from a docstring. They are not evidence for any of our hypotheses.
- **Not the monolithic-trend framing.** qlib is sometimes described as a platform. A
  platform invites extension by accretion — one more module that knows about all the
  others. We keep the loose coupling and refuse the platform framing.

## Epistemic status

**Secondary, architectural.** These claims come from the user's supplied research
notes describing qlib's pipeline and its automated-alpha work, plus general knowledge of
the project's public architecture; I did not read the qlib repository in this session.
No file paths, line numbers or version-specific claims are asserted, and none of
qlib's performance figures are cited or relied upon.

A future agent should treat this document as a *structural* precedent — evidence about
how to decompose a research system, of the same standing as the architectural
conventions documented in our own schema comments. It carries **no** evidentiary weight
about any market effect. See [methodology_references.md](methodology_references.md) for
where qlib sits in the tier hierarchy, and [finrobot.md](finrobot.md) for the other
architectural principle qlib assumes but does not itself enforce.

## Related

- [finrobot.md](finrobot.md) — who is allowed to validate what (reasoning vs. computation)
- [tradingagents.md](tradingagents.md) — temporal integrity of the evidence itself
- [methodology_references.md](methodology_references.md) — source tiers and the discovery index
- [../methods/HYPOTHESIS_SCHEMA.md](../methods/HYPOTHESIS_SCHEMA.md) — what the formal language must express
- [../methods/failure_modes.md](../methods/failure_modes.md) — why rejection must be a recorded result
- [../moondev/rbi_methodology.md](../moondev/rbi_methodology.md) — the Research/Backtest/Implement split our chain follows
- [../research_integrity/hypothesis_discovery_loop.md](../research_integrity/hypothesis_discovery_loop.md) — why we deliberately run no alpha search loop
- [../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md](../research_integrity/RESEARCH_INTEGRITY_CHECKLIST.md) — the operative pre-enqueue gate
- [../research_integrity/reproducibility_audit.md](../research_integrity/reproducibility_audit.md) — what `research_identity` must carry to be auditable