# The closed research loop

```
SOURCE ──► EVIDENCE ──► MECHANISM ──► HYPOTHESIS ──► FORMAL RULESPEC
                                                           │
                        EXPERIMENT QUEUE ◄── INTEGRITY GATE ◄── CRITIC
                                │
════════════════════════════════╪════════════ LAYER 1 / LAYER 2 BOUNDARY
                                ▼
                          RBI ADAPTER  ──►  external engine (Moon Dev)
                                │
                          EXPERIMENT RUN
                                │
                      EXPERIMENT RESULT  (typed columns)
                                │
                          RESULT ANALYST
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        negative          new hypothesis      experiment
         knowledge        (revision)          trace (DAG)
              │                 │
              └────────► back to HYPOTHESIS ◄───────┘
```

Layer 1 never crosses the boundary downward, and Layer 2 never decides what to
trade. The arrow out of the analyst back to `HYPOTHESIS` is what makes this a
research system rather than a backtest runner.

## The stage order, and the one deliberate deviation

```
source → evidence → mechanism → hypothesis → formalize → critique → integrity → queue
```

A hypothesis is **formalized before it is critiqued**, which inverts the order
you might expect from the diagram. The critic is asked two questions — is the
signal computable, and does it leak future information — and neither is
answerable about a claim that has no rule yet. Formalization is what turns a
claim into something those questions can be asked of.

The ordering is enforced at runtime in `layer1/pipeline.py`. `run_pipeline` is
the supported way to move a source through the system; the individual
component functions remain public for testing and partial re-runs, but a stage
transition reached any other way records nothing about why it happened.

## What the evidence stage is for

Before this stage, the system was `source → AI → idea`. Now it is:

```
source → typed evidence → mechanism → hypothesis
```

Every extracted assertion is stored with a `claim_type`: `fact`,
`author_claim`, `empirical_result`, or `model_inference`. Anything the model
itself produced goes through `evidence.record_model_inference()`, whose
signature has **no `claim_type` parameter at all** — so no version of that call
can present a model's reading as a source's finding.

That distinction is the whole reason the table exists. A research database that
collapses "the paper found X" into "X" cannot answer whether X is true, only
whether X was ever written down.

## Why the integrity gate is a gate and not a check

Layer 1 does not own datasets. It cannot invent a temporal split, a universe, or
a cost model. So `run_pipeline` audits each hypothesis against the protocol
supplied by the caller, and a hypothesis with **no** protocol audits
`incomplete` and is **not queued**.

That is the intended behaviour, not a gap. An under-specified protocol produces
irreproducible results — the defect the Agentic Trading survey measured in 17
of 19 qualifying studies. The gate fails closed: it takes the *latest* audit
across all statuses and only then filters to `pass`, so a hypothesis that
passed and was later downgraded is withdrawn from the queue automatically.

Verified behaviour:

| Input | Audit | Queued |
|---|---|---|
| no protocol | `incomplete`, 15 missing fields | no |
| complete protocol | `pass` | yes |
| passed, then re-audited as `fail` | latest = `fail` | no |

## The result contract

A backtest's output is **never** an informal blob of stdout. `experiment_results`
holds typed columns; `experiment_artifacts.stdout` exists for forensics only and
a consumer must never have to parse it. If a headline number cannot be selected
by name, it has not been recorded — it has been narrated.

Two columns exist because of specific research findings:

- **`net_return`** — not `gross_return`. A gross number that survives no costs
  is not a finding.
- **`mechanism_consistent`** — whether the result is consistent with the
  mechanism the hypothesis *claimed*. This is why Layer 1 stores
  `expected_mechanism`: a strategy that made money for reasons unrelated to the
  stated mechanism is not support for that hypothesis. Positive return is not
  alpha; a run can be paid entirely by market beta.

`multiple_testing_adjusted` is a column rather than a footnote because an
uncorrected p-value after sweeping hundreds of hypotheses is close to
meaningless, and a consumer must be able to see that from the row alone.

## The adapter port

`layer2/rbi_adapter.py` defines the seam where an external engine attaches:

```python
class ExperimentAdapter(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def run(self, spec: ExperimentSpec) -> RunOutcome: ...
```

Moon Dev's RBI engine implements this. Moonbot does not contain it, does not
reimplement it, and does not decide what to trade — RBI is an *experiment
engine*, not the brain. The Harvard RBI methodology is research doctrine
(`knowledge/moondev/`); Moon Dev's executable tooling sits behind this port.

**`execution_backends.is_real_engine` is `false` for the stub.** A stub produces
well-formed but meaningless numbers, and a result whose backend is not a real
engine must never be presented as evidence. That flag exists so an operator can
see, at a glance, that this system has not yet run a real engine.

## The return path

A measurement is not a conclusion. `layer2/analyst.py` turns results into
verdicts, and the verdict is what writes knowledge back into Layer 1:

| Condition | Verdict |
|---|---|
| no net return, or effect dies after costs | `refute` |
| positive net return, **mechanism inconsistent** | `revise` — *not* `support` |
| positive net return, mechanism consistent | `support` |
| unfair test (zero trades, no cost model, unresolved dataset) | `inconclusive` |

Verdicts are determined by **deterministic code**, never by an LLM's opinion. An
LLM may write the prose `reasoning` afterwards; it never decides the verdict. A
critic that can be talked into a verdict by a persuasive prompt is not a critic.

The `refute` verdict writes into Layer 1's `failures` table with
`stage='layer2_backtest'` and a specific `failure_type` — not a generic
"failed". The Research Critic reads that table before critiquing anything new,
so the second generation of hypotheses is stricter than the first. That is the
loop closing.

## Experiment trace

Research history is a DAG, not a list:

```
H001 ──┬── run A (refuted)
       ├── run B (promising) ──► H001a ──► run C
       └── run D (contradiction)
```

Each `experiment_trace` row records *why* a run was attempted given what its
parent showed. `rationale` is `NOT NULL` because an unjustified edge is a rerun,
not progress — and without it the question "this family has been revised 7 times
and still fails out of sample" is unanswerable.

## Running it

```bash
# Layer 1 only — stops at the queue
L1_LLM_MODE=deterministic python examples/ofi_pipeline.py

# The full loop, including a stub run and an analyst verdict
python examples/closed_loop.py
```

Layer 1 stops at the experiment queue. Nothing downstream of that line is
required to generate an idea.