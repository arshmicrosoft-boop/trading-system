"""Layer 2 -- the Result Analyst: the loop's return path.

A measurement is not a conclusion.  ``experiment_results`` says what a run
produced; it does not say what we now believe.  This module is what reads the
measurement and writes a VERDICT, and the verdict is what writes knowledge
back into Layer 1 -- a ``refute`` becomes a row in Layer 1's ``failures``
table, which is the record the hypothesis generator reads before it produces
the next generation.

Without this step the loop is open.  Results accumulate, every one of them
looks equally valid, and nothing reads them.  That is not a smaller system; it
is a backtest runner with a schema.

Two rules shape every line below.

**1. The verdict is DETERMINISTIC CODE.  An LLM may write the prose, never
the verdict.**  Every threshold and every mapping below is arithmetic in this
file: net return sign, trade count, p-value, cost model, mechanism
consistency.  ``analyse`` takes an optional ``judge`` callable, and the judge
is handed the finished verdict to *re-word*: only ``reasoning`` (and
``mechanism_verdict``) may come back from it.  The kind, the validity and the
invalidity reason are taken from code and a judge that returns different ones
raises :class:`VerdictMutationError` instead of being obeyed.  The refusal is
structural, not a prompt instruction, because a research critic that can be
talked into a verdict by a persuasive prompt is not a critic -- it is a mirror
for whatever the last persuasive argument was.  The architecture should make
that failure impossible to express, not merely discouraged.

**2. Test validity is checked FIRST, before any metric is read.**  A verdict
is a claim about what a run *showed*, and an unfair test shows nothing.  If the
run has no net return, no trades, no cost model, an unresolved dataset version,
an uncorrected p-value from a parameter sweep, or no measured attribution on a
profitable result, the verdict is ``inconclusive`` with ``test_valid = False``
and a stated reason.  This ordering is not defensive decoration: a ``support``
derived from an unfair test is *worse* than no verdict, because it launders a
methodological flaw into evidence that the next generation will treat as fact.

Note the asymmetry that ordering produces, and it is deliberate.  A losing run
does not need attribution to be refuted -- a negative return after costs is a
measurement whether or not we can say what produced it -- so the attribution
check fires only on the *profitable* branch.  Requiring attribution before
reading a loss would turn every refutation into an invalid test, and would
throw away exactly the negative knowledge this module exists to record.

THE MOST IMPORTANT MAPPING IN THIS FILE
---------------------------------------
Positive net return with ``mechanism_consistent = False`` is ``revise``, never
``support``.  This is the KTD-Fin attribution lesson
(``knowledge/research_integrity/knowledge_leakage.md``) and it is the reason
Layer 1 stores ``expected_mechanism`` at proposal time.  A strategy can earn a
return from market beta, a style exposure, or one lucky regime, entirely
unrelated to the causal path the hypothesis claimed -- and a return figure is
the easiest number in the world to produce and the least informative about why
it happened.  Attribution is only possible against a *claimed mechanism*, and
if nobody recorded one at proposal time the question can never be asked after
the fact.  So: the money is real, the hypothesis is not confirmed, and the
honest verdict is that the direction may be worth pursuing while the stated
mechanism is wrong.  ``support`` means "this earned its money the way it said
it would", which is a stronger claim than "this made money" and the only one
worth writing into the corpus.

Mapping, in the order applied.  Validity, then the measurement:

======================================================  ==================
condition                                               kind
======================================================  ==================
run not completed, errored, untyped, or unattributed     inconclusive
(``test_valid = False``)
fewer than :data:`MIN_DISCRIMINATING_TRADES` trades      inconclusive
``net_return <= 0`` after costs                          refute
``gross_return > 0 >= net_return``                       refute (cost death)
``p_value >= :data:`ALPHA``                              refute
engine ``outcome = 'refuted'``                           refute
engine ``outcome = 'inconclusive'``                      inconclusive
``net_return > 0``, ``mechanism_consistent = False``     **revise**
``net_return > 0``, ``mechanism_consistent = True``      support
======================================================  ==================

An engine outcome that is *more pessimistic* than the arithmetic is honoured
(``refuted``/``inconclusive``); a more optimistic one never upgrades a verdict
the metrics do not support.  A runner that reports ``supported`` for a negative
net return is wrong, and the metrics outrank it.

WHY THE FAILURE TYPE IS CHOSEN CAREFULLY
----------------------------------------
A ``refute`` writes into Layer 1's ``failures`` table, whose enum describes
*reasoning* defects ("the mechanism has no account of why it should exist"),
while a refutation is a *data* event -- the idea was tested and it was wrong.
Conflating them is why ``stage`` exists, and why this module always writes
``stage = 'layer2_backtest'``.  The enum value chosen is the closest honest
one from :data:`REFUTE_FAILURE_TYPES`; the truth is in the ``reason`` prose,
which always names the recorded numbers, never just "did not work".

This module implements no engine, holds no exchange credentials, and decides
nothing about what to trade.  It reads typed result rows and draws a
conclusion from them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from layer1 import db
from layer1.components.queueing import PENDING_DATA_VERSION

__all__ = [
    "ALPHA",
    "MIN_DISCRIMINATING_TRADES",
    "REFUTE_FAILURE_TYPES",
    "TRACE_EDGE_REASONS",
    "UNRESOLVED_DATASET_VERSIONS",
    "AnalystError",
    "UnknownRunError",
    "VerdictMutationError",
    "TraceLinkError",
    "Verdict",
    "analyse",
    "record_verdict",
    "latest_verdict",
    "list_verdicts",
    "knowledge_for_next_generation",
    "link_run",
]


# ---------------------------------------------------------------------
# Thresholds.  Named, module-level, and explained.
# ---------------------------------------------------------------------
#
# Every one of these is a property of the TEST, never a profitability target.
# `ALPHA` is a significance level: it says whether an observed effect is
# distinguishable from noise.  It says nothing about how large the effect
# should be, and there is deliberately no threshold here that a return has to
# clear to be "good" -- Layer 1 holds no such number and Layer 2's job here is
# to report what was measured, not to grade it.

#: Significance level. A p-value at or above this does not survive the test.
ALPHA: float = 0.05

#: Minimum trades for the sample to be able to discriminate anything.
#:
#: Below roughly thirty independent trades the standard error of a return
#: estimate exceeds any plausible effect size, so a "win" and a loss are not
#: statistically different events.  A verdict built on fewer trades than this
#: is a verdict about luck, which is why the threshold gates ``inconclusive``
#: rather than ``refute``: too few trades says the test was blind, not that the
#: hypothesis is false.
MIN_DISCRIMINATING_TRADES: int = 30

#: Dataset versions that name no snapshot, so a run pinned to one is not
#: reproducible.  Mirrors the protocol check in :mod:`layer1.integrity` --
#: "latest" is a moving target, and a result that cannot be re-derived because
#: the thing that produced it no longer exists under that name is not a
#: result.  ``PENDING_DATA_VERSION`` is the sentinel Layer 1 writes when it
#: queues a hypothesis without owning a dataset, so a run still carrying it
#: never had its data pinned at all.
UNRESOLVED_DATASET_VERSIONS: frozenset[str] = frozenset(
    {
        PENDING_DATA_VERSION,
        "latest",
        "current",
        "live",
        "rolling",
        "unknown",
        "n/a",
    }
)

#: The Layer 1 ``failure_type`` values a backtest refutation may be recorded
#: as.  ``unformalizable`` and ``malformed_llm_output`` are excluded on
#: purpose: they describe artifacts that never became an experiment, so a
#: Layer 2 refutation of that kind would be a category error.
REFUTE_FAILURE_TYPES: tuple[str, ...] = (
    "not_falsifiable",
    "not_measurable",
    "implausible_mechanism",
    "future_information_leak",
    "not_distinct",
    "hidden_discretion",
    "prior_failure_precedent",
)

#: The reasons ``experiment_trace`` accepts, validated here so the error names
#: the legal values instead of surfacing a cast failure with no context.
TRACE_EDGE_REASONS: tuple[str, ...] = (
    "initial",
    "revision",
    "generalization",
    "specialization",
    "parameter_sweep",
    "replication",
    "contradiction",
    "data_correction",
)

VerdictKind = Literal["support", "refute", "revise", "replicate", "inconclusive"]


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class AnalystError(RuntimeError):
    """A request the analyst cannot honour as stated."""


class UnknownRunError(AnalystError):
    """A run or hypothesis id that does not exist."""


class VerdictMutationError(AnalystError):
    """An injected judge tried to change a field that only code may set."""


class TraceLinkError(AnalystError):
    """An experiment-trace edge that would make the DAG meaningless."""


# ---------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------


class Verdict(BaseModel):
    """What we now believe about one run.

    ``extra="forbid"`` for the same reason ``RuleSpec`` has it: a verdict with
    an unrecognised field is a verdict whose meaning this module cannot check.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    kind: VerdictKind
    reasoning: str = Field(min_length=1)
    test_valid: bool = True
    invalidity_reason: str | None = None
    mechanism_verdict: str | None = None
    spawned_hypothesis_id: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Verdict:
        """A verdict of "invalid" that does not say why is not a verdict.

        Mirrors the database CHECK ``invalid_run_must_explain``.  The check is
        not redundant: it also fires for rows written by SQL that never went
        through this model.
        """
        if not self.test_valid and not (self.invalidity_reason or "").strip():
            raise ValueError(
                "test_valid=False requires an invalidity_reason: 'invalid' "
                "without a stated cause tells the next generation nothing it "
                "can act on"
            )
        return self


# ---------------------------------------------------------------------
# Id minting
# ---------------------------------------------------------------------
#
# `db/schema_experiment.sql` defines sequences for the Layer 1 id families and
# for `hypothesis_versions`, but NONE for `analyst_verdicts.verdict_id` or
# `experiment_trace.edge_id` -- and adding one would mean editing the SQL file
# that is the schema of record, which this change is not permitted to touch.
# The same reasoning `layer1/evidence.py` applies to claim ids: a sequence
# would serialise every writer across processes to produce an ordering nobody
# consumes, when the only requirement is uniqueness.  So these are random
# tokens, and every one carries a prefix so a reader can tell at a glance that
# the column is not sequence-backed.  Reads order by `created_at`, never by id.


def _mint(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# ---------------------------------------------------------------------
# Reading one run
# ---------------------------------------------------------------------
#
# The `experiment_run_report` view is deliberately NOT used here.  It
# LEFT JOINs `analyst_verdicts` on `run_id`, and a run may legitimately carry
# more than one verdict (a re-analysis after a data correction), so the view
# returns one row per VERDICT, not per run.  Reading a run through it would
# make the number of rows depend on how many times the run had been judged.

_RUN_COLUMNS = """
    r.run_id, r.experiment_id, r.hypothesis_id, r.research_id,
    r.backend, r.status, r.outcome,
    r.dataset, r.dataset_version, r.cost_model, r.execution_model,
    r.parameters, r.failure_type, r.failure_reason,
    r.code_hash, r.config_hash, r.spec_hash,
    ri.parameter_space,
    h.statement AS hypothesis_statement, h.expected_mechanism,
    b.is_real_engine,
    res.gross_return, res.net_return, res.trade_count,
    res.p_value, res.multiple_testing_adjusted, res.mechanism_consistent,
    res.residual_alpha, res.market_exposure, res.stability, res.regime_dependence
"""


def _load_run(cur, run_id: str) -> dict[str, Any] | None:
    """One run with its result, its provenance and its engine's honesty flag."""
    cur.execute(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM experiment_runs r
        JOIN research_identity ri ON ri.research_id = r.research_id
        JOIN hypotheses h ON h.hypothesis_id = r.hypothesis_id
        LEFT JOIN experiment_results res ON res.run_id = r.run_id
        LEFT JOIN execution_backends b ON b.backend = r.backend
        WHERE r.run_id = %s
        """,
        (run_id,),
    )
    return cur.fetchone()


def _fetch_run(run_id: str) -> dict[str, Any]:
    """`_load_run` in its own transaction, for callers that hold none."""
    with db.tx() as cur:
        row = _load_run(cur, run_id)
    if row is None:
        raise UnknownRunError(f"no experiment run {run_id!r}")
    return row


def _is_swept(run: dict[str, Any]) -> bool:
    """Whether this run came out of a search over variants.

    ``research_identity.parameter_space`` is the count Layer 1 declared; the
    run's own ``parameters`` is what the engine actually varied.  Either being
    non-empty means the best of several variants is what got reported, and an
    uncorrected p-value from a best-of-N is close to meaningless.
    """
    return bool(run["parameter_space"]) or bool(run["parameters"])


def _has_cost_model(run: dict[str, Any]) -> bool:
    """Whether the run declared what trading cost it was charged.

    ``cost_model`` defaults to ``'{}'`` rather than NULL, so absence shows up
    as an empty object.  An empty one means gross and net have no declared
    relationship, and a net return from a run that never said what it paid is
    a gross number wearing a net label.
    """
    return bool(run["cost_model"])


def _dataset_resolved(run: dict[str, Any]) -> bool:
    version = (run["dataset_version"] or "").strip()
    return bool(version) and version.lower() not in UNRESOLVED_DATASET_VERSIONS


def _latest_failure(cur, hypothesis_id: str) -> dict[str, Any] | None:
    """The most recent negative record for a hypothesis, in the caller's tx."""
    cur.execute(
        "SELECT failure_id, failure_type, stage, reason FROM failures "
        "WHERE hypothesis_id = %s ORDER BY created_at DESC, failure_id DESC "
        "LIMIT 1",
        (hypothesis_id,),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------
#
# `_decide` is the whole module's judgement, and it is pure arithmetic over one
# row.  It returns the kind, the validity, the invalidity reason, the
# mechanism verdict, the failure type and the sentence that explains all of
# it.  Nothing in it reads prose, calls a model, or depends on wall-clock time,
# so the same row always yields the same verdict.


def _fmt(value: float | None, places: int = 4) -> str:
    return "unmeasured" if value is None else f"{value:.{places}f}"


def _header(run: dict[str, Any]) -> str:
    """One identifying clause naming what was actually run."""
    dataset = run["dataset_version"] or "an unpinned dataset"
    engine = run["backend"]
    honest = "" if run["is_real_engine"] else " [NOT A REAL ENGINE]"
    return (
        f"{engine} run {run['run_id']} on {dataset}{honest} "
        f"(status={run['status']}, engine_outcome={run['outcome'] or 'none'})"
    )


def _decide(run: dict[str, Any]) -> dict[str, Any]:
    """Map one measured run to a verdict.  Deterministic; see the module docstring."""
    head = _header(run)
    net = run["net_return"]
    gross = run["gross_return"]
    trades = run["trade_count"]
    p_value = run["p_value"]
    mechanism_ok = run["mechanism_consistent"]

    # -- 1. Validity, before any metric is read. ---------------------
    invalid: list[str] = []
    if run["status"] != "completed":
        invalid.append(f"run status is {run['status']!r}, not 'completed'")
    if run["outcome"] == "errored":
        invalid.append("the engine reported outcome 'errored'")
    if net is None:
        invalid.append(
            "no typed net_return was recorded, so there is no measurement to judge"
        )
    if trades == 0:
        invalid.append("the run placed no trades, so it tested nothing")
    if not _has_cost_model(run):
        invalid.append(
            "no cost model was declared, so 'net' is a gross number under a net label"
        )
    if not _dataset_resolved(run):
        invalid.append(
            f"dataset_version {run['dataset_version']!r} names no snapshot, "
            "so the run cannot be reproduced"
        )
    if p_value is not None and not run["multiple_testing_adjusted"] and _is_swept(run):
        invalid.append(
            f"p_value {_fmt(p_value, 3)} was not corrected for having tested "
            "multiple variants of this rule"
        )
    if invalid:
        reason = "; ".join(invalid)
        return {
            "kind": "inconclusive",
            "test_valid": False,
            "invalidity_reason": reason,
            "mechanism_verdict": (
                "no conclusion is available: this run was not a fair test"
            ),
            "failure_type": None,
            "reasoning": (
                f"{head}. The run could not discriminate between this hypothesis "
                f"and its alternatives, so no verdict about the hypothesis is "
                f"available from it: {reason}."
            ),
        }

    # -- 2. The sample must be big enough to say anything. -------------
    if trades is None or trades < MIN_DISCRIMINATING_TRADES:
        seen = "no trade count" if trades is None else f"{trades} trades"
        return {
            "kind": "inconclusive",
            "test_valid": True,
            "invalidity_reason": None,
            "mechanism_verdict": (
                f"the run was fair but blind: {seen} is below the "
                f"{MIN_DISCRIMINATING_TRADES} needed to tell an effect from luck"
            ),
            "failure_type": None,
            "reasoning": (
                f"{head}. The test itself was fair -- costs, data and provenance "
                f"were all declared -- but it saw {seen}, below the "
                f"{MIN_DISCRIMINATING_TRADES} independent trades needed to "
                f"discriminate. net_return {_fmt(net)} is therefore not evidence "
                f"in either direction."
            ),
        }

    # -- 3. Negative after costs, or an effect that does not survive
    #       them.  Both are refutations. ------------------------------
    if net is not None and net <= 0:
        cost_death = gross is not None and gross > 0
        failure_type = "not_measurable" if cost_death else "not_falsifiable"
        detail = (
            f"the effect was present before costs (gross_return {_fmt(gross)}) "
            f"and gone after them (net_return {_fmt(net)}), so it is not "
            f"measurable net of this cost regime"
            if cost_death
            else (
                f"no effect was present to measure: gross_return {_fmt(gross)}, "
                f"net_return {_fmt(net)}. The predicted relationship did not "
                f"appear in the data"
            )
        )
        mechanism = (
            "the stated mechanism did not produce a return that survived costs"
        )
        reasoning = (
            f"{head}. Refuted: {detail}. Recorded as failure_type "
            f"{failure_type!r} at stage 'layer2_backtest' so the next generation "
            f"is generated in the light of it."
        )
        if run["outcome"] == "refuted":
            reasoning += " The engine had already reported this run as refuted."
        return {
            "kind": "refute",
            "test_valid": True,
            "invalidity_reason": None,
            "mechanism_verdict": mechanism,
            "failure_type": failure_type,
            "reasoning": reasoning,
        }

    # -- 4. A p-value that does not survive. ------------------------
    if p_value is not None and p_value >= ALPHA:
        return {
            "kind": "refute",
            "test_valid": True,
            "invalidity_reason": None,
            "mechanism_verdict": (
                "the observed effect is indistinguishable from noise at the "
                f"{ALPHA} significance level"
            ),
            "failure_type": "not_falsifiable",
            "reasoning": (
                f"{head}. Refuted on significance rather than on sign: net_return "
                f"{_fmt(net)} is positive but p_value {_fmt(p_value, 3)} does not "
                f"survive {ALPHA}"
                + (
                    " even after correction for the variants searched."
                    if run["multiple_testing_adjusted"]
                    else "."
                )
            ),
        }

    if run["outcome"] == "refuted":
        return {
            "kind": "refute",
            "test_valid": True,
            "invalidity_reason": None,
            "mechanism_verdict": (
                "the engine refuted the hypothesis under conditions it could "
                "declare; this module honours the more pessimistic reading"
            ),
            "failure_type": run["failure_type"]
            if run["failure_type"] in REFUTE_FAILURE_TYPES
            else "not_falsifiable",
            "reasoning": (
                f"{head}. Refuted: the engine reported outcome 'refuted' and its "
                f"failure diagnosis is {run['failure_type'] or 'unrecorded'}"
                f" ({run['failure_reason'] or 'no reason recorded'})."
            ),
        }

    # -- 5. The engine declined to decide.  Honoured, because a runner
    #       that knows its own test was blind is better informed than
    #       the arithmetic on that point. ---------------------------
    if run["outcome"] == "inconclusive":
        return {
            "kind": "inconclusive",
            "test_valid": True,
            "invalidity_reason": None,
            "mechanism_verdict": (
                "the engine judged its own test unable to discriminate, and that "
                "self-report outranks the arithmetic on this point"
            ),
            "failure_type": None,
            "reasoning": (
                f"{head}. Inconclusive: the engine reported outcome "
                f"'inconclusive' ({run['failure_reason'] or 'no reason recorded'}) "
                f"even though net_return {_fmt(net)} is positive."
            ),
        }

    # -- 6. Profitable.  Attribution decides whether this is support or
    #       a revision, and it is checked here rather than earlier
    #       because a loss never needs it. -------------------------
    if mechanism_ok is None:
        return {
            "kind": "inconclusive",
            "test_valid": False,
            "invalidity_reason": (
                "the run measured a positive net return but recorded no "
                "mechanism attribution, so the return cannot be separated from "
                "market beta, a style exposure or one regime"
            ),
            "mechanism_verdict": (
                "positive return is not alpha, and an unattributed one cannot "
                "speak to the claimed mechanism either way"
            ),
            "failure_type": None,
            "reasoning": (
                f"{head}. The run could not discriminate: net_return {_fmt(net)} "
                f"is positive, but mechanism_consistent was never measured"
                + (
                    f" (residual_alpha {_fmt(run['residual_alpha'])}, market "
                    f"exposure {_fmt(run['market_exposure'])})"
                    if run["residual_alpha"] is not None or run["market_exposure"] is not None
                    else ""
                )
                + ". A return with no attribution cannot support a claim about a "
                "mechanism, and calling it support would record a number as "
                "knowledge."
            ),
        }

    if mechanism_ok is False:
        return {
            "kind": "revise",
            "test_valid": True,
            "invalidity_reason": None,
            "mechanism_verdict": (
                "the return did not come from the stated mechanism, so the causal "
                "path claimed at proposal time is not the one that operated"
            ),
            "failure_type": None,
            "reasoning": (
                f"{head}. Revised, not supported: net_return {_fmt(net)} is "
                f"positive, but mechanism_consistent is False. The strategy made "
                f"money for reasons other than the mechanism this hypothesis "
                f"claimed -- the money is real, the explanation is wrong. This is "
                f"the attribution lesson from KTD-Fin: a raw return cannot "
                f"distinguish reasoning from exposure, and 'expected_mechanism' is "
                f"recorded at proposal time precisely so this question can be "
                f"answered. A follow-up hypothesis should keep the direction and "
                f"correct the mechanism."
            ),
        }

    return {
        "kind": "support",
        "test_valid": True,
        "invalidity_reason": None,
        "mechanism_verdict": (
            "the observed return is consistent with the claimed mechanism and "
            "survives costs"
        ),
        "failure_type": None,
        "reasoning": (
            f"{head}. Supported: net_return {_fmt(net)} is positive after the "
            f"declared cost model, over {trades} trades, and the result is "
            f"consistent with the mechanism this hypothesis claimed. No failure "
            f"is recorded -- a support verdict writes negative knowledge for "
            f"nothing."
        ),
    }


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def _apply_judge(verdict: Verdict, judge: Callable[[Verdict], Verdict | str]) -> Verdict:
    """Let ``judge`` re-word a finished verdict, and nothing else.

    The judge is a prose channel.  It is handed the completed verdict and its
    ``reasoning`` and ``mechanism_verdict`` are adopted; every other field is
    rebuilt from the code-determined verdict.  A judge that returns a different
    kind, validity or invalidity reason is not corrected silently -- that would
    hide the attempt -- it raises :class:`VerdictMutationError`, so a
    persuasion attempt fails loudly instead of succeeding quietly.
    """
    returned = judge(verdict)
    if isinstance(returned, str):
        returned = verdict.model_copy(update={"reasoning": returned})

    for field in ("kind", "test_valid", "invalidity_reason"):
        if getattr(returned, field) != getattr(verdict, field):
            raise VerdictMutationError(
                f"judge attempted to change {field!r} from "
                f"{getattr(verdict, field)!r} to {getattr(returned, field)!r}; "
                "the verdict is determined by the metric arithmetic in "
                "layer2.analyst._decide and a judge may only write prose"
            )

    return verdict.model_copy(
        update={
            "reasoning": returned.reasoning,
            "mechanism_verdict": returned.mechanism_verdict,
        }
    )


def analyse(
    run_id: str, *, judge: Callable[[Verdict], Verdict | str] | None = None
) -> Verdict:
    """Turn one run's measurements into a verdict.

    Reads ``experiment_runs`` joined to ``experiment_results`` and writes
    nothing.  The determination is code; ``judge``, if given, may only re-word
    the ``reasoning`` and ``mechanism_verdict`` it already carries.

    :raises UnknownRunError: if ``run_id`` does not exist.
    :raises VerdictMutationError: if ``judge`` returns different code-owned
        fields.
    """
    run = _fetch_run(run_id)
    verdict = Verdict(
        run_id=run_id,
        hypothesis_id=run["hypothesis_id"],
        **_verdict_fields(_decide(run)),
    )
    return _apply_judge(verdict, judge) if judge is not None else verdict


def _verdict_fields(decision: dict[str, Any]) -> dict[str, Any]:
    """Project a decision dict onto :class:`Verdict` fields.

    ``failure_type`` is deliberately dropped here: it belongs to the negative
    knowledge written at record time, not to the verdict, and carrying it on the
    verdict would invite a caller to present a refutation's diagnosis without
    the refutation.
    """
    return {
        "kind": decision["kind"],
        "reasoning": decision["reasoning"],
        "test_valid": decision["test_valid"],
        "invalidity_reason": decision["invalidity_reason"],
        "mechanism_verdict": decision["mechanism_verdict"],
    }


def _failure_type_for(cur, verdict: Verdict, run: dict[str, Any]) -> tuple[str, str]:
    """Choose WHY a refuted hypothesis died, as a Layer 1 enum value plus prose.

    The rule this function exists to enforce comes from
    ``knowledge/methods/failure_modes.md``: "'it didn't work' is not a reason".
    A row whose reason restates its verdict has recorded nothing -- the next
    generator reads it as closed and does not learn what to do differently.  So
    the enum value is picked from what the run actually measured, and the
    prose always carries the numbers.

    The ladder runs most-specific first, and every rung has to be earned by the
    record rather than guessed:

    1. a diagnosis the runner already made -- the engine that saw the failure
       at the point it happened knows more than this module reading it after;
    2. a previous recorded failure -- a second refutation of the same
       hypothesis is precisely what ``prior_failure_precedent`` names;
    3. otherwise ``not_falsifiable``, the conservative reading of a predicted
       relationship that did not appear.  Claiming a more specific defect we
       cannot support would be a worse lie than a general one.
    """
    # What the code-level decision already concluded about the cause: the cost
    # ladder distinguishes an edge that vanished under costs
    # (`not_measurable`) from one that never existed (`not_falsifiable`).
    base = _decide(run)["failure_type"] or "not_falsifiable"

    recorded = (run["failure_type"] or "").strip()
    if recorded in REFUTE_FAILURE_TYPES:
        return recorded, (
            f"the runner diagnosed this failure as {recorded!r}: "
            f"{run['failure_reason'] or 'no reason recorded'}"
        )

    prior = _latest_failure(cur, verdict.hypothesis_id)
    if prior is not None:
        return "prior_failure_precedent", (
            f"this hypothesis was already recorded as failing at stage "
            f"{prior['stage']!r} ({prior['failure_type']}: {prior['reason']}), "
            f"and this run refuted it again"
        )

    return base, verdict.reasoning


#: Columns persisted by INSERT.  `verdict_id` is minted here, never read back
#: into the model.
_VERDICT_INSERT_COLUMNS = (
    "verdict_id, run_id, hypothesis_id, kind, reasoning, test_valid, "
    "invalidity_reason, mechanism_verdict, spawned_hypothesis_id"
)

#: Columns the model owns.  Deliberately excludes `verdict_id`: `Verdict` is
#: `extra="forbid"`, so validating a row that carried the id would raise on a
#: field the model does not model.  Ordering is always `created_at DESC`, and
#: ids are random tokens, so the id is never an ordering key.
_VERDICT_COLUMNS = (
    "run_id, hypothesis_id, kind, reasoning, test_valid, "
    "invalidity_reason, mechanism_verdict, spawned_hypothesis_id"
)


def record_verdict(verdict: Verdict) -> str:
    """Persist a verdict, and write the negative knowledge it implies.

    Returns the ``verdict_id``.  The two writes happen in ONE transaction, and
    that is the whole point of this function: a ``refute`` that recorded a
    verdict without recording a failure would leave the loop open again, so the
    failure row is not an optional consequence of the verdict -- it is part of
    the same statement.  ``support`` writes no failure, because a failure
    database that records successes is a log, not knowledge.

    A second verdict of the *same kind* for the same run is refused: re-running
    the analysis would otherwise duplicate the same negative knowledge and make
    the failure count a measure of how often the analyst was called.

    :raises UnknownRunError: if the run does not exist.
    :raises AnalystError: if the run was already judged this way.
    """
    with db.tx() as cur:
        run = _load_run(cur, verdict.run_id)
        if run is None:
            raise UnknownRunError(f"no experiment run {verdict.run_id!r}")
        if run["hypothesis_id"] != verdict.hypothesis_id:
            raise AnalystError(
                f"run {verdict.run_id!r} belongs to hypothesis "
                f"{run['hypothesis_id']!r}, not {verdict.hypothesis_id!r}"
            )

        cur.execute(
            "SELECT 1 FROM analyst_verdicts WHERE run_id = %s AND kind = %s::verdict_kind "
            "LIMIT 1",
            (verdict.run_id, verdict.kind),
        )
        if cur.fetchone() is not None:
            raise AnalystError(
                f"run {verdict.run_id!r} already has a {verdict.kind!r} verdict; "
                "record a different kind only when something in the evidence has "
                "actually changed (a data correction, a new protocol)"
            )

        if verdict.spawned_hypothesis_id is not None:
            cur.execute(
                "SELECT 1 FROM hypotheses WHERE hypothesis_id = %s",
                (verdict.spawned_hypothesis_id,),
            )
            if cur.fetchone() is None:
                raise UnknownRunError(
                    f"spawned_hypothesis_id {verdict.spawned_hypothesis_id!r} does "
                    "not exist; a verdict may only name a follow-up hypothesis "
                    "that was actually created"
                )

        verdict_id = _mint("VER")
        cur.execute(
            f"""
            INSERT INTO analyst_verdicts ({_VERDICT_INSERT_COLUMNS})
            VALUES (%s, %s, %s, %s::verdict_kind, %s, %s, %s, %s, %s)
            RETURNING verdict_id
            """,
            (
                verdict_id,
                verdict.run_id,
                verdict.hypothesis_id,
                verdict.kind,
                verdict.reasoning,
                verdict.test_valid,
                verdict.invalidity_reason,
                verdict.mechanism_verdict,
                verdict.spawned_hypothesis_id,
            ),
        )

        # Read BEFORE the insert: after it, this run's own failure row would be
        # the "most recent" one and every refutation would report itself as its
        # own precedent.
        if verdict.kind == "refute":
            failure_type, why = _failure_type_for(cur, verdict, run)
            cur.execute(
                "INSERT INTO failures (hypothesis_id, reason, failure_type, stage, "
                "reported_by) VALUES (%s, %s, %s::failure_type, 'layer2_backtest', %s)",
                (
                    verdict.hypothesis_id,
                    f"{verdict.reasoning} [refuted on {verdict.run_id}] {why}",
                    failure_type,
                    "layer2.result_analyst",
                ),
            )
    return verdict_id


def latest_verdict(hypothesis_id: str) -> Verdict | None:
    """The most recent verdict on a hypothesis, or None if it has never run."""
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_VERDICT_COLUMNS} FROM analyst_verdicts "
            "WHERE hypothesis_id = %s ORDER BY created_at DESC, verdict_id DESC "
            "LIMIT 1",
            (hypothesis_id,),
        )
        row = cur.fetchone()
    return Verdict.model_validate(row) if row else None


def list_verdicts(
    hypothesis_id: str | None = None, kind: str | None = None, limit: int = 100
) -> list[Verdict]:
    """Read verdicts, newest first.  Both filters are optional and combine."""
    capped = max(1, min(int(limit), 1000))
    clauses: list[str] = []
    params: list[Any] = []
    if hypothesis_id is not None:
        clauses.append("hypothesis_id = %s")
        params.append(hypothesis_id)
    if kind is not None:
        clauses.append("kind = %s::verdict_kind")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(capped)

    with db.tx() as cur:
        cur.execute(
            f"SELECT {_VERDICT_COLUMNS} FROM analyst_verdicts {where} "
            "ORDER BY created_at DESC, verdict_id DESC LIMIT %s",
            params,
        )
        return [Verdict.model_validate(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------
# Feeding the next generation
# ---------------------------------------------------------------------


def _stamp(row: dict[str, Any]) -> dict[str, Any]:
    """Make a row JSON-serialisable so the result can be handed to a generator."""
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()
    }


def knowledge_for_next_generation(limit: int = 50) -> dict[str, Any]:
    """Assemble what the hypothesis generator must know before it proposes.

    Four things, and the fourth is the one that makes this a loop rather than a
    report:

    * ``failures`` -- every recorded negative result, newest first, each with
      its specific reason. A generator that reads only "this failed" produces
      the same idea again with fresh confidence, because nothing in the string
      told it what to do differently.
    * ``refuted_mechanisms`` -- mechanisms killed by data more than once, with
      the failure types that killed them. This is the "we have tested this
      economic story seven times" signal.
    * ``contradictions`` -- unresolved conflicts between claims, which are leads
      rather than failures.
    * ``prior_failure_precedents`` -- the explicit precedent rows, which is the
      answer to the ``prior_failure_collision`` question: has this idea already
      died here, under these conditions?

    Every list is capped by ``limit`` so a mature corpus cannot hand the
    generator more than it can use.
    """
    capped = max(1, min(int(limit), 1000))

    with db.tx() as cur:
        cur.execute(
            """
            SELECT f.failure_id, f.hypothesis_id, f.failure_type, f.stage,
                   f.reported_by, f.reason, f.created_at,
                   h.mechanism_id, h.statement AS hypothesis_statement,
                   h.expected_mechanism
            FROM failures f
            JOIN hypotheses h ON h.hypothesis_id = f.hypothesis_id
            ORDER BY f.created_at DESC, f.failure_id DESC
            LIMIT %s
            """,
            (capped,),
        )
        failures = [_stamp(r) for r in cur.fetchall()]

        # One row per mechanism, not one per failure. The `failures` list above
        # already carries every reason in full; this rollup answers the
        # narrower question "how often has this story been killed, and how",
        # so it aggregates the small enum set in full and takes only the most
        # recent reason verbatim. Aggregating every reason here would grow the
        # payload without corpus size and restate text the caller has.
        cur.execute(
            """
            SELECT h.mechanism_id,
                   count(*) AS refutations,
                   count(DISTINCT f.hypothesis_id) AS hypotheses,
                   array_agg(DISTINCT f.failure_type::text) AS failure_types,
                   (array_agg(f.reason ORDER BY f.created_at DESC, f.failure_id DESC))[1]
                       AS latest_reason
            FROM failures f
            JOIN hypotheses h ON h.hypothesis_id = f.hypothesis_id
            WHERE f.stage = 'layer2_backtest'
            GROUP BY h.mechanism_id
            ORDER BY refutations DESC, h.mechanism_id
            LIMIT %s
            """,
            (capped,),
        )
        refuted_mechanisms = [_stamp(r) for r in cur.fetchall()]

        # `possible_reasons` is a conflict_reason[] -- a Postgres enum array,
        # which psycopg hands back as the raw literal '{market,regime}'.  The
        # cast is the same one layer1/evidence.py makes, for the same reason.
        cur.execute(
            """
            SELECT cc.contradiction_id, cc.possible_reasons::text[] AS possible_reasons,
                   cc.resolution_status, cc.spawned_hypothesis_id, cc.created_at,
                   cc.claim_a AS claim_a_id, cc.claim_b AS claim_b_id,
                   a.statement AS claim_a_statement, b.statement AS claim_b_statement,
                   a.source_id AS claim_a_source, b.source_id AS claim_b_source
            FROM claim_contradictions cc
            JOIN claims a ON a.claim_id = cc.claim_a
            JOIN claims b ON b.claim_id = cc.claim_b
            ORDER BY (cc.resolution_status = 'unresolved') DESC, cc.created_at DESC
            LIMIT %s
            """,
            (capped,),
        )
        contradictions = [_stamp(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT f.failure_id, f.hypothesis_id, f.reason, f.created_at,
                   h.mechanism_id, h.statement AS hypothesis_statement,
                   h.expected_mechanism
            FROM failures f
            JOIN hypotheses h ON h.hypothesis_id = f.hypothesis_id
            WHERE f.failure_type = 'prior_failure_precedent'
            ORDER BY f.created_at DESC, f.failure_id DESC
            LIMIT %s
            """,
            (capped,),
        )
        precedents = [_stamp(r) for r in cur.fetchall()]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "failures": failures,
        "refuted_mechanisms": refuted_mechanisms,
        "contradictions": contradictions,
        "prior_failure_precedents": precedents,
        "counts": {
            "failures": len(failures),
            "refuted_mechanisms": len(refuted_mechanisms),
            "contradictions": len(contradictions),
            "prior_failure_precedents": len(precedents),
        },
    }


# ---------------------------------------------------------------------
# The trace DAG
# ---------------------------------------------------------------------


def link_run(
    parent_run_id: str,
    child_run_id: str,
    *,
    reason: str,
    rationale: str,
    child_hypothesis_id: str | None = None,
) -> str:
    """Record why one run was attempted because of another.  Returns ``edge_id``.

    ``rationale`` is required and the requirement is not pedantry: an edge with
    no stated reason makes the trace a list of runs rather than a research
    history, and the finding this table exists to support -- "this family has
    been revised seven times and still fails out of sample" -- becomes
    unanswerable without one.

    A self-loop is refused here as well as by the database CHECK, because an
    error that names what went wrong beats a constraint violation that names a
    table.

    :raises TraceLinkError: on a self-loop, an unknown reason, or an empty
        rationale.
    :raises UnknownRunError: if either run, or the child hypothesis, is absent.
    """
    if parent_run_id == child_run_id:
        raise TraceLinkError(
            f"run {parent_run_id!r} cannot be its own parent; a self-loop is a "
            "run that justifies itself, which is the opposite of a lineage"
        )
    if reason not in TRACE_EDGE_REASONS:
        raise TraceLinkError(
            f"unknown trace edge reason {reason!r}; valid values are "
            f"{sorted(TRACE_EDGE_REASONS)}"
        )
    text = (rationale or "").strip()
    if not text:
        raise TraceLinkError(
            "an edge needs a rationale: without it this is a rerun, not progress"
        )

    with db.tx() as cur:
        cur.execute(
            "SELECT run_id FROM experiment_runs WHERE run_id = ANY(%s) FOR SHARE",
            ([parent_run_id, child_run_id],),
        )
        present = {r["run_id"] for r in cur.fetchall()}
        missing = [r for r in (parent_run_id, child_run_id) if r not in present]
        if missing:
            raise UnknownRunError(f"no experiment run(s) {missing}")
        if child_hypothesis_id is not None:
            cur.execute(
                "SELECT 1 FROM hypotheses WHERE hypothesis_id = %s", (child_hypothesis_id,)
            )
            if cur.fetchone() is None:
                raise UnknownRunError(f"no hypothesis {child_hypothesis_id!r}")

        edge_id = _mint("EDGE")
        cur.execute(
            "INSERT INTO experiment_trace (edge_id, parent_run_id, child_run_id, "
            "child_hypothesis_id, reason, rationale) "
            "VALUES (%s, %s, %s, %s, %s::trace_edge_reason, %s) RETURNING edge_id",
            (edge_id, parent_run_id, child_run_id, child_hypothesis_id, reason, text),
        )
    return edge_id

