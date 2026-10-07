"""The run lifecycle: claim an experiment, record what came back.

This is the write path, so it is strict by construction.  Every transition is
validated against the row it is about to change, in the same transaction as
the change, because a run row that claims to have completed without a result
is worse than a run that never started: it is a confident-looking artefact
that later gets read as evidence.

The invariants this module exists to hold:

* **One hypothesis, one rule.**  `start_run` re-derives the identity of the
  experiment from the queue row and refuses if the three foreign keys
  (`hypothesis_id`, `version_id`, `research_id`) do not refer to the same
  thing.  A run that tests H-abc's claim with H-xyz's spec would produce a
  coherent, well-formed, wrong result -- the most expensive kind of wrong,
  because nothing downstream can tell.
* **Nothing is tested under conditions it was not queued for.**  The
  controlled-experiment metadata defaults from the queued job, so a caller
  cannot silently test on a different data version or parameter set than the
  one the queue recorded.  An override must be passed deliberately, and the
  run then records that it was.
* **A dataset must be pinned before it can be run against.**  Layer 1 has no
  market data and cannot choose one, so it queues with the sentinel
  `pending_l2_dataset_selection`.  Resolving that sentinel is precisely Layer
  2's job, and it must be resolved explicitly -- an unidentified dataset
  makes the result irreproducible, and an irreproducible result is a rumour.
* **A completed run has typed metrics.**  `experiment_results` columns are
  written by name, in the same transaction as the status transition.  A metric
  the schema has no column for is moved to `extra_metrics` with a note, never
  dropped and never parked in a log line.
* **A failure always carries a reason.**  `fail_run` and a `failed` outcome
  both refuse to write without one.  A failure nobody can read is not a
  record, it is a shrug.

No market data, no exchange client, no order or position concept lives here.
The engine that would need them attaches at the port in
:mod:`layer2.rbi_adapter`.

Identifier allocation
---------------------
`db/schema_experiment.sql` defines no Postgres sequence for `run_id`,
`verdict_id` or `edge_id`, unlike Layer 1's SRC/MECH/H/EXP prefixes which go
through `ids.next_id()`.  So this module allocates them in Python:
``RUN_{uuid4().hex[:12]}``.  A uuid4 suffix cannot collide between concurrent
runners without a shared counter, and the 12-hex-char slice keeps ids short
enough to read in a log line while leaving 48 bits of entropy.  Adding the
sequences would mean editing `db/*.sql`, which is not this module's call:
the schema is owned as a whole, and this module documents the gap instead of
quietly patching around it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from layer1 import db
from layer1.components.queueing import PENDING_DATA_VERSION
from layer1.contracts import ExperimentJob

from .rbi_adapter import RunOutcome

__all__ = [
    "BACKENDS",
    "RunStateError",
    "RunRecord",
    "RESULT_COLUMNS",
    "UNMAPPED_KEY",
    "start_run",
    "claim_next",
    "complete_run",
    "fail_run",
    "get_run",
    "list_runs",
    "run_report",
]

RunStatus = Literal["pending", "running", "completed", "failed", "invalidated"]
ResultOutcome = Literal["supported", "refuted", "inconclusive", "errored"]
BackendName = Literal["stub", "moondev_rbi", "external"]

#: The `execution_backend` enum.  A run cannot name a backend the type does
#: not have, so this is checked before the insert rather than surfacing as an
#: opaque cast failure from the driver.
BACKENDS: frozenset[str] = frozenset({"stub", "moondev_rbi", "external"})


class RunStateError(RuntimeError):
    """A run-state request that must not be honoured.

    Raised instead of writing a run row inconsistent with the queue, or
    advancing a run that is not in a state to advance.  Callers are expected
    to let it propagate: a refused write is information, not an error to
    swallow.
    """


# ---------------------------------------------------------------------
# Metric mapping -- typed columns, never prose
# ---------------------------------------------------------------------

#: `metrics` keys that ARE real columns, grouped by storage class.  These are
#: the only names a headline number may arrive under; everything else lands in
#: `extra_metrics`.
FLOAT_COLUMNS: tuple[str, ...] = (
    "gross_return",
    "net_return",
    "volatility",
    "max_drawdown",
    "sharpe",
    "sortino",
    "turnover",
    "hit_rate",
    "avg_holding_bars",
    "stability",
    "regime_dependence",
    "p_value",
    "market_exposure",
    "residual_alpha",
)
INT_COLUMNS: tuple[str, ...] = ("trade_count",)
BOOL_COLUMNS: tuple[str, ...] = ("multiple_testing_adjusted", "mechanism_consistent")
JSONB_COLUMNS: tuple[str, ...] = ("factor_exposure",)

#: Every column of `experiment_results` a metric may address, by name.
RESULT_COLUMNS: frozenset[str] = frozenset(
    FLOAT_COLUMNS + INT_COLUMNS + BOOL_COLUMNS + JSONB_COLUMNS
)

#: Positional order of the columns written by `complete_run`'s INSERT.
_RESULT_COLUMN_ORDER: tuple[str, ...] = FLOAT_COLUMNS + INT_COLUMNS + BOOL_COLUMNS + JSONB_COLUMNS

#: Reserved key in `extra_metrics` naming the metrics that had no column.  The
#: point is that nothing an engine produced is ever silently lost: if it cannot
#: be selected by name, at least the record shows it was set aside.
UNMAPPED_KEY = "_unmapped"


def _as_bool(metric: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise RunStateError(
        f"metric {metric!r} is a BOOLEAN column but the engine reported {value!r}; "
        "send true/false (or 1/0)"
    )


def _as_int(metric: str, value: Any) -> int:
    if isinstance(value, bool):
        raise RunStateError(f"metric {metric!r} is an INTEGER column, not a flag")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise RunStateError(
        f"metric {metric!r} is an INTEGER column but the engine reported {value!r}"
    )


def _map_metrics(outcome: RunOutcome) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split an outcome into (typed columns, extra_metrics).

    `metrics` is float-valued, so the integer and boolean columns arrive as
    numbers and are coerced strictly: `trade_count = 3.5` is a broken engine,
    not a value to round.  A metric naming no column is preserved under
    `extra_metrics` with its name recorded in `UNMAPPED_KEY`, so a number the
    schema has nowhere to put is visible rather than invisible.
    """
    columns: dict[str, Any] = {}
    aside: dict[str, Any] = {}
    for metric, value in outcome.metrics.items():
        if metric in FLOAT_COLUMNS:
            columns[metric] = float(value)
        elif metric in INT_COLUMNS:
            columns[metric] = _as_int(metric, value)
        elif metric in BOOL_COLUMNS:
            columns[metric] = _as_bool(metric, value)
        else:
            aside[metric] = value

    extra: dict[str, Any] = dict(outcome.extra_metrics)
    # `factor_exposure` is JSONB, so it cannot arrive through the float-valued
    # `metrics` map; it is promoted out of `extra_metrics` when the engine put
    # it there, which keeps it selectable by name like every other metric.
    for name in JSONB_COLUMNS:
        if name in extra:
            columns[name] = extra.pop(name)

    if aside:
        extra.update(aside)
        already = extra.get(UNMAPPED_KEY)
        listed = list(already) if isinstance(already, list) else []
        extra[UNMAPPED_KEY] = listed + sorted(aside)
    return columns, extra


def _result_value(columns: Mapping[str, Any], name: str) -> Any:
    """One column value, adapted to how psycopg sends it.

    Two things have to happen here, and both are about the difference between
    "absent" and "empty", which the schema does not treat as the same thing:

    * `factor_exposure` is JSONB, so a dict must be wrapped in `Jsonb` or the
      driver cannot adapt it.  It is also NOT NULL, and a column DEFAULT only
      applies when the column is omitted -- this INSERT names every column
      positionally, so an explicit NULL would be a constraint violation.  The
      substitute is the honest one: an empty map means "no factor attribution
      was measured", which a reader can tell apart from a measured zero.
    * `multiple_testing_adjusted` is NOT NULL too.  Absent means "this p-value
      was not corrected", which is False -- and False is exactly the fact the
      schema comment insists a consumer must be able to see from the row.
    """
    value = columns.get(name)
    if name == "factor_exposure":
        return db.Jsonb(value if value is not None else {})
    if value is not None:
        return value
    if name == "multiple_testing_adjusted":
        return False
    return None


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------

_RUN_COLUMNS = """
    r.run_id, r.experiment_id, r.hypothesis_id, r.research_id, q.version_id,
    r.dataset, r.dataset_version, r.universe, r.time_period, r.timeframe,
    r.feature_version, r.execution_model, r.cost_model, r.parameters,
    r.random_seed, r.backend::text AS backend, r.status::text AS status,
    r.outcome::text AS outcome, r.code_hash, r.config_hash, r.spec_hash,
    r.failure_type, r.failure_reason, r.started_at, r.finished_at, r.created_at,
    res.gross_return, res.net_return, res.volatility, res.max_drawdown,
    res.sharpe, res.sortino, res.trade_count, res.turnover, res.hit_rate,
    res.avg_holding_bars, res.stability, res.regime_dependence, res.p_value,
    res.multiple_testing_adjusted, res.market_exposure, res.factor_exposure,
    res.residual_alpha, res.mechanism_consistent, res.extra_metrics
"""

#: `version_id` lives on the queue row rather than on `experiment_runs`, so the
#: read joins the queue to recover the identity quadruple.  That is deliberate:
#: the version is frozen at enqueue time by a trigger, so a run can always
#: recover which spec revision it was authorised against.
_RUN_FROM = (
    "FROM experiment_runs r "
    "JOIN experiment_queue q ON q.experiment_id = r.experiment_id "
    "LEFT JOIN experiment_results res ON res.run_id = r.run_id"
)

_JOB_COLUMNS = """
    experiment_id, research_id, hypothesis_id, version_id, feature_set,
    rule_version, data_version, parameter_space, status, queued_at,
    hypothesis_statement, expected_mechanism, rule_spec, source_ids,
    model_used, prompt_version, creation_timestamp
"""


class RunRecord(BaseModel):
    """One attempt at testing one hypothesis, as stored.

    Identity and controlled-experiment metadata come from `experiment_runs`;
    the metric block is the LEFT JOIN onto `experiment_results` and is all
    `None` until the run completes.  Both are on one model so a caller can
    tell "measured zero" from "never measured" without a second query -- the
    distinction between a refuted hypothesis and an unrun one.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    experiment_id: str
    hypothesis_id: str
    research_id: str
    version_id: int

    dataset: str | None = None
    dataset_version: str | None = None
    universe: dict[str, Any] = Field(default_factory=dict)
    time_period: dict[str, Any] | None = None
    timeframe: str | None = None
    feature_version: str | None = None

    execution_model: dict[str, Any] = Field(default_factory=dict)
    cost_model: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    random_seed: int | None = None

    backend: BackendName
    status: RunStatus
    outcome: ResultOutcome | None = None

    code_hash: str | None = None
    config_hash: str | None = None
    spec_hash: str | None = None

    failure_type: str | None = None
    failure_reason: str | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None

    gross_return: float | None = None
    net_return: float | None = None
    volatility: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    trade_count: int | None = None
    turnover: float | None = None
    hit_rate: float | None = None
    avg_holding_bars: float | None = None
    stability: float | None = None
    regime_dependence: float | None = None
    p_value: float | None = None
    multiple_testing_adjusted: bool | None = None
    market_exposure: float | None = None
    factor_exposure: dict[str, Any] | None = None
    residual_alpha: float | None = None
    mechanism_consistent: bool | None = None
    extra_metrics: dict[str, Any] | None = None


def _record(row: Mapping[str, Any]) -> RunRecord:
    return RunRecord.model_validate(dict(row))


# ---------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------


def _new_run_id() -> str:
    """``RUN_<12 hex>`` -- see the module docstring on why there is no sequence."""
    return f"RUN_{uuid4().hex[:12]}"


def _fetch_job(cur, experiment_id: str) -> ExperimentJob | None:
    """The queued job, read through the Layer 1 hand-off view."""
    cur.execute(
        f"SELECT {_JOB_COLUMNS} FROM experiment_queue_layer2 WHERE experiment_id = %s",
        (experiment_id,),
    )
    row = cur.fetchone()
    return ExperimentJob.model_validate(row) if row else None


def _assert_provenance_agrees(cur, job: ExperimentJob) -> None:
    """Check that the job's three identity foreign keys refer to one thing.

    `experiment_queue` stores `hypothesis_id`, `version_id` and `research_id`
    as three independent foreign keys, and the hand-off view inner-joins all
    three.  A row mixing H-abc with H-xyz's spec revision therefore surfaces to
    Layer 2 as a coherent-looking job that tests the wrong thing, and nothing
    in the schema objects.  So the agreement is checked rather than trusted.

    This is the same three-way check Layer 1 applies at enqueue time
    (`queueing._assert_provenance_agrees`), re-applied at claim time for two
    reasons: the queue is a hand-off surface any writer can touch, and this is
    the last point before real compute is spent on the answer.  Three indexed
    lookups against a wrong, confident result row is a good trade.

    The final check -- that the spec payload names the same hypothesis -- closes
    the gap the foreign keys leave open: the keys agreeing is necessary, but
    the spec text is what the engine actually reads.
    """
    cur.execute(
        "SELECT hypothesis_id FROM hypothesis_versions WHERE version_id = %s",
        (job.version_id,),
    )
    version_row = cur.fetchone()
    if version_row is None:
        raise RunStateError(
            f"queued job {job.experiment_id} names unknown version {job.version_id}"
        )
    if version_row["hypothesis_id"] != job.hypothesis_id:
        raise RunStateError(
            f"queued job {job.experiment_id} mixes identities: version {job.version_id} "
            f"belongs to hypothesis {version_row['hypothesis_id']!r}, not "
            f"{job.hypothesis_id!r}. Refusing to run a job whose rule is not its "
            "own hypothesis's rule"
        )

    cur.execute(
        "SELECT hypothesis_id, rule_version FROM research_identity WHERE research_id = %s",
        (job.research_id,),
    )
    identity_row = cur.fetchone()
    if identity_row is None:
        raise RunStateError(
            f"queued job {job.experiment_id} names unknown research identity {job.research_id}"
        )
    if identity_row["hypothesis_id"] != job.hypothesis_id:
        raise RunStateError(
            f"research identity {job.research_id} belongs to hypothesis "
            f"{identity_row['hypothesis_id']!r}, not {job.hypothesis_id!r}"
        )
    if identity_row["rule_version"] != job.rule_version:
        raise RunStateError(
            f"research identity {job.research_id} pins rule_version "
            f"{identity_row['rule_version']!r}, not {job.rule_version!r}"
        )

    spec = job.rule_spec or {}
    spec_hypothesis = spec.get("hypothesis_id") if isinstance(spec, Mapping) else None
    if spec_hypothesis is not None and spec_hypothesis != job.hypothesis_id:
        raise RunStateError(
            f"queued job {job.experiment_id} carries a rule spec written for hypothesis "
            f"{spec_hypothesis!r} but claims hypothesis {job.hypothesis_id!r}. Refusing to "
            "test one hypothesis's claim with another one's rule"
        )


def _require_backend_usable(cur, backend: str) -> None:
    """Refuse to start a run on a backend the database says is unavailable.

    `execution_backends.is_available` is the operator-visible record of what
    this system can actually run, and `is_real_engine` records whether it has
    ever run anything real.  Starting a run against a backend flagged
    unavailable produces a row that looks executable and is not.
    """
    cur.execute(
        "SELECT is_available FROM execution_backends WHERE backend::text = %s",
        (backend,),
    )
    row = cur.fetchone()
    if row is None:
        raise RunStateError(
            f"backend {backend!r} has no row in execution_backends; a backend must be "
            "registered there before a run may name it"
        )
    if not row["is_available"]:
        raise RunStateError(
            f"backend {backend!r} is marked is_available = false in execution_backends; "
            "a run cannot be started against it"
        )


def _resolve_dataset_version(job: ExperimentJob, dataset_version: str | None) -> str:
    """Pin the data version, refusing an unresolved sentinel.

    Layer 1 owns no market data and so cannot choose a dataset; it queues with
    the sentinel `pending_l2_dataset_selection` and says so plainly rather than
    guessing.  A run started against the sentinel would record a result nobody
    can reproduce, and an irreproducible result is a rumour.  Resolution
    happens here, at claim time, and it has to be passed in -- nothing in this
    repository knows which datasets exist, so there is nothing to default it to.
    """
    resolved = dataset_version if dataset_version is not None else job.data_version
    if not resolved or not resolved.strip():
        raise RunStateError(
            f"queued job {job.experiment_id} has no data_version; Layer 2 must pin a "
            "dataset_version before a run can start"
        )
    resolved = resolved.strip()
    if resolved == PENDING_DATA_VERSION:
        raise RunStateError(
            f"queued job {job.experiment_id} still carries the sentinel data_version "
            f"{PENDING_DATA_VERSION!r}. Dataset selection is Layer 2's responsibility, so a "
            "real dataset_version must be passed explicitly to start_run(); an "
            "unidentified dataset makes the result irreproducible"
        )
    return resolved


def _spec_hash(job: ExperimentJob) -> str | None:
    """Content hash of the queued rule spec, so the run records what it ran."""
    if not job.rule_spec:
        return None
    canonical = json.dumps(job.rule_spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _universe_from_spec(job: ExperimentJob) -> dict[str, Any]:
    """Carry the rule's own market block forward as the run's universe."""
    spec = job.rule_spec or {}
    market = spec.get("market") if isinstance(spec, Mapping) else None
    return {str(k): v for k, v in market.items()} if isinstance(market, Mapping) else {}


def _finish(cur, run_id: str) -> RunRecord:
    cur.execute(f"SELECT {_RUN_COLUMNS} {_RUN_FROM} WHERE r.run_id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        raise RunStateError(f"run {run_id} vanished mid-transaction")
    return _record(row)


def _advance_queue(cur, experiment_id: str, to_status: str, column: str) -> None:
    """Move the Layer 1 queue row forward.

    Only `status`, `claimed_at` and `completed_at` are writable here -- a
    trigger freezes everything Layer 1 wrote, so the identity of an experiment
    cannot drift after it was queued.  The advance is idempotent because
    `experiment_runs` deliberately allows several runs per experiment (a sweep,
    a replication, a re-run after a data correction); a later run must not be
    blocked by an earlier one having moved the queue on already.
    """
    cur.execute(
        f"UPDATE experiment_queue SET status = %s::experiment_status, "
        f"{column} = COALESCE({column}, now()) "
        "WHERE experiment_id = %s AND status::text <> %s",
        (to_status, experiment_id, to_status),
    )


def _write_status(
    cur,
    run_id: str,
    *,
    status: str,
    outcome: str,
    failure_type: str | None,
    failure_reason: str | None,
) -> None:
    cur.execute(
        "UPDATE experiment_runs SET status = %s::experiment_run_status, "
        "outcome = %s::result_outcome, failure_type = %s, failure_reason = %s, "
        "finished_at = now() WHERE run_id = %s",
        (status, outcome, failure_type, failure_reason, run_id),
    )


def _write_artifacts(cur, run_id: str, code_hash: str | None, outcome: RunOutcome) -> None:
    """Store stdout/stderr for forensics. Never the record of what happened.

    The row is only written when there is something to keep: an artifact row
    full of NULLs looks like evidence was preserved when none was.  `code_hash`
    is carried from the run so the workspace on disk and the code that produced
    the numbers stay tied together.
    """
    if not (outcome.stdout or outcome.stderr or outcome.artifacts or outcome.workspace_path):
        return
    cur.execute(
        "INSERT INTO experiment_artifacts (run_id, workspace_path, stdout, stderr, "
        "code_hash, artifacts) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id) DO UPDATE SET workspace_path = EXCLUDED.workspace_path, "
        "stdout = EXCLUDED.stdout, stderr = EXCLUDED.stderr, "
        "code_hash = EXCLUDED.code_hash, artifacts = EXCLUDED.artifacts",
        (
            run_id,
            outcome.workspace_path,
            outcome.stdout,
            outcome.stderr,
            code_hash,
            db.Jsonb(dict(outcome.artifacts)),
        ),
    )


# ---------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------


def start_run(
    experiment_id: str,
    *,
    backend: str = "stub",
    dataset: str | None = None,
    dataset_version: str | None = None,
    universe: Mapping[str, Any] | None = None,
    time_period: Mapping[str, Any] | None = None,
    timeframe: str | None = None,
    feature_version: str | None = None,
    execution_model: Mapping[str, Any] | None = None,
    cost_model: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
    random_seed: int | None = None,
    code_hash: str | None = None,
    config_hash: str | None = None,
    spec_hash: str | None = None,
) -> RunRecord:
    """Claim a queued experiment and open a run against it.

    The controlled-experiment metadata DEFAULTS from the queued job:
    `dataset_version` from `data_version`, `parameters` from
    `parameter_space`, `universe` from the rule spec's market block,
    `spec_hash` from the rule spec itself.  A caller who means to test under
    different conditions has to say so, and the run then records that they did.
    The default is the queued condition precisely so the ordinary case cannot
    drift from what Layer 1 recorded.

    The exception is `dataset_version`: it defaults from the job, but the job
    may still carry Layer 1's sentinel, and then this raises.  See
    `_resolve_dataset_version`.

    On success the run is `running` and the queue row has advanced to `claimed`
    in the same transaction -- a claim that is not atomic with the run row it
    authorises is a claim that can be lost.
    """
    if backend not in BACKENDS:
        raise RunStateError(f"backend {backend!r} is not one of {sorted(BACKENDS)}")

    with db.tx() as cur:
        job = _fetch_job(cur, experiment_id)
        if job is None:
            raise RunStateError(
                f"experiment {experiment_id!r} is not in the Layer 1 queue; only a "
                "critic-approved hypothesis may be run"
            )
        if job.status not in ("queued", "claimed"):
            raise RunStateError(
                f"experiment {experiment_id} is {job.status!r}; Layer 2 may only claim a "
                "queued or previously claimed experiment"
            )

        _assert_provenance_agrees(cur, job)
        _require_backend_usable(cur, backend)
        pinned = _resolve_dataset_version(job, dataset_version)

        run_id = _new_run_id()
        cur.execute(
            """
            INSERT INTO experiment_runs (
                run_id, experiment_id, hypothesis_id, research_id,
                dataset, dataset_version, universe, time_period, timeframe,
                feature_version, execution_model, cost_model, parameters,
                random_seed, backend, status, code_hash, config_hash, spec_hash,
                started_at
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s::execution_backend, 'running', %s, %s, %s,
                now()
            )
            """,
            (
                run_id,
                job.experiment_id,
                job.hypothesis_id,
                job.research_id,
                dataset,
                pinned,
                db.Jsonb(dict(universe) if universe is not None else _universe_from_spec(job)),
                db.Jsonb(dict(time_period)) if time_period is not None else None,
                timeframe,
                feature_version,
                db.Jsonb(dict(execution_model or {})),
                db.Jsonb(dict(cost_model or {})),
                db.Jsonb(dict(parameters) if parameters is not None else dict(job.parameter_space)),
                random_seed,
                backend,
                code_hash,
                config_hash,
                spec_hash if spec_hash is not None else _spec_hash(job),
            ),
        )
        _advance_queue(cur, job.experiment_id, "claimed", "claimed_at")
        return _finish(cur, run_id)


def claim_next(status_filter: str = "queued") -> list[ExperimentJob]:
    """The queued experiments Layer 2 may claim, oldest first.

    A read, not a write.  Claiming happens inside `start_run`, atomically with
    the run row it authorises, so the database is never left in a state where
    a job is claimed but no run exists.  This returns the work list for a
    runner; it is the Layer 1 queue, read through the hand-off view, so a
    runner needs no knowledge of Layer 1's table layout.
    """
    from layer1.components import queueing

    return queueing.list_queue(status=status_filter, limit=100)


def complete_run(run_id: str, outcome: RunOutcome) -> RunRecord:
    """Record a terminal result: status transition and metrics in one transaction.

    The run must be `running`.  A run cannot complete if it never started, and
    a `completed` run with no `experiment_results` row is a claim of evidence
    that does not exist -- so both are written here, together, or not at all.

    A completed run must report `net_return`.  A gross number that survives no
    costs is a marketing number, and the schema's own comment says so; a run
    reporting only gross is declining to say whether the idea paid for its own
    execution, which is the one question an experiment exists to answer.

    An outcome with `status = "failed"` is accepted here as well, so a runner
    has one terminal write path.  It requires `failure_reason`, sets the run to
    `failed` / `errored`, and records no metrics: a broken run has no result,
    and an empty result row would let a failure be read as a flat finding.
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT run_id, experiment_id, code_hash, status::text AS status "
            "FROM experiment_runs WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        run = cur.fetchone()
        if run is None:
            raise RunStateError(f"unknown run {run_id!r}")
        if run["status"] != "running":
            raise RunStateError(
                f"run {run_id} is {run['status']!r}, not 'running'; a run cannot complete "
                "unless it started and has not already finished"
            )

        if outcome.status == "failed":
            reason = (outcome.failure_reason or "").strip()
            if not reason:
                raise RunStateError(
                    f"run {run_id} reported status='failed' with no failure_reason; a "
                    "failure without a reason is unauditable"
                )
            _write_status(
                cur,
                run_id,
                status="failed",
                outcome="errored",
                failure_type=(outcome.failure_type or "").strip() or "unspecified",
                failure_reason=reason,
            )
            _write_artifacts(cur, run_id, run["code_hash"], outcome)
            _advance_queue(cur, run["experiment_id"], "completed", "completed_at")
            return _finish(cur, run_id)

        if outcome.outcome == "errored":
            raise RunStateError(
                "a completed run cannot carry outcome 'errored'; use status='failed' with "
                "a failure_reason so the break is recorded rather than hidden"
            )

        columns, extra = _map_metrics(outcome)
        if columns.get("net_return") is None:
            raise RunStateError(
                f"run {run_id} reported no net_return. gross_return without net_return is a "
                "marketing number; if the run has no measured return then it did not "
                "complete and should be failed with a reason"
            )

        _write_status(
            cur,
            run_id,
            status="completed",
            outcome=outcome.outcome,
            failure_type=outcome.failure_type,
            failure_reason=outcome.failure_reason,
        )
        # The column list is the single source of truth for the statement, and
        # the placeholders are derived from it.  Counting them separately --
        # which is what this used to do -- is how a results INSERT ends up
        # with 19 placeholders and 20 parameters, and therefore no result rows
        # at all.  Deriving both from one list means adding a column cannot
        # desynchronise them.
        result_columns = ("run_id", *_RESULT_COLUMN_ORDER, "extra_metrics")
        cur.execute(
            f"""
            INSERT INTO experiment_results ({", ".join(result_columns)})
            VALUES ({", ".join(["%s"] * len(result_columns))})
            ON CONFLICT (run_id) DO UPDATE SET
                {", ".join(f"{name} = EXCLUDED.{name}" for name in _RESULT_COLUMN_ORDER)},
                extra_metrics = EXCLUDED.extra_metrics,
                recorded_at = now()
            """,
            (run_id, *[_result_value(columns, name) for name in _RESULT_COLUMN_ORDER], db.Jsonb(extra)),
        )
        _write_artifacts(cur, run_id, run["code_hash"], outcome)
        _advance_queue(cur, run["experiment_id"], "completed", "completed_at")
        return _finish(cur, run_id)


def fail_run(run_id: str, failure_type: str, failure_reason: str) -> RunRecord:
    """Record that a run broke, with the reason. The reason is not optional.

    A failure nobody can diagnose is not a record; it is a shrug, and it cost
    the same compute as one that was explained.  `status = 'failed'` and
    `outcome = 'errored'` are written together, because 'errored' alone leaves
    "the experiment broke" and "the row failed to write" indistinguishable.

    This is the path for a runner that broke before it produced a
    `RunOutcome` at all.  No `experiment_results` row is written: there is no
    result, and an empty one would read as a flat finding.
    """
    kind = (failure_type or "").strip()
    reason = (failure_reason or "").strip()
    if not kind:
        raise RunStateError("failure_type is required: a failure must say what kind it was")
    if not reason:
        raise RunStateError("failure_reason is required: a failure without a reason is unauditable")

    with db.tx() as cur:
        cur.execute(
            "SELECT status::text AS status FROM experiment_runs WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RunStateError(f"unknown run {run_id!r}")
        if row["status"] not in ("pending", "running"):
            raise RunStateError(
                f"run {run_id} is {row['status']!r}; only a run that has not finished can "
                "be failed"
            )
        _write_status(
            cur, run_id, status="failed", outcome="errored", failure_type=kind, failure_reason=reason
        )
        return _finish(cur, run_id)


def get_run(run_id: str) -> RunRecord | None:
    """One run with its metrics, or None."""
    with db.tx() as cur:
        cur.execute(f"SELECT {_RUN_COLUMNS} {_RUN_FROM} WHERE r.run_id = %s", (run_id,))
        row = cur.fetchone()
        return _record(row) if row else None


def list_runs(
    experiment_id: str | None = None,
    hypothesis_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[RunRecord]:
    """Runs, newest first, optionally filtered by experiment, hypothesis or status.

    "H019 failed" is not a fact; "H019 failed on these three runs" is.  That
    distinction is why several runs per experiment are allowed, and why this
    reads rows rather than one verdict per hypothesis.
    """
    capped = max(1, min(int(limit), 1000))
    clauses: list[str] = []
    params: list[Any] = []
    if experiment_id is not None:
        clauses.append("r.experiment_id = %s")
        params.append(experiment_id)
    if hypothesis_id is not None:
        clauses.append("r.hypothesis_id = %s")
        params.append(hypothesis_id)
    if status is not None:
        clauses.append("r.status::text = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(capped)

    with db.tx() as cur:
        cur.execute(
            f"SELECT {_RUN_COLUMNS} {_RUN_FROM} {where} "
            "ORDER BY r.created_at DESC, r.run_id DESC LIMIT %s",
            params,
        )
        return [_record(row) for row in cur.fetchall()]


def run_report(run_id: str) -> dict | None:
    """The single-row read surface for one run: claim, metadata, metrics, verdict.

    Reads `experiment_run_report`, which deliberately excludes stdout.  If a
    consumer reaches this view and finds the number it wants is not in the row,
    the answer is that the number was never recorded -- not that the log should
    be parsed.
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT * FROM experiment_run_report WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None