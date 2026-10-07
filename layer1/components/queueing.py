"""The Layer 1 / Layer 2 boundary.

Layer 1 writes here; Layer 2 reads here.  A row in `experiment_queue` is the
only thing that crosses the boundary, and it can only exist for a hypothesis
the Research Critic passed.

Nothing in this module backtests, simulates, prices, sizes, or executes.  It
does not know what a P&L is.  Deciding whether a mechanism has evidence is
Layer 2's job; this module's only power is the right to refuse.
"""

from __future__ import annotations

from typing import Any, get_args

from .. import db, ids
from ..contracts import ExperimentJob, FailureRecord, FailureType

__all__ = [
    "PENDING_DATA_VERSION",
    "QueueInvariantError",
    "enqueue",
    "list_queue",
    "record_failure",
    "list_failures",
]

#: Layer 1 does not own, fetch, or version datasets -- it has no market data
#: access at all.  So it cannot know which dataset an experiment needs, and
#: refuses to pretend it does: the job is queued with this sentinel and Layer 2
#: pins a real dataset when it claims the job.  A queued row carrying this
#: sentinel is explicitly "not yet reproducible"; LAYER2.md says a result
#: without a pinned data_version is not a result.
PENDING_DATA_VERSION = "pending_l2_dataset_selection"

_VALID_FAILURE_TYPES = frozenset(get_args(FailureType))

# `failures.stage` is CHECK-constrained in the schema; validating here turns a
# raw psycopg error into an error that names the valid values.
_VALID_STAGES = frozenset({"critic", "layer2_backtest"})

_JOB_COLUMNS = """
    experiment_id, research_id, hypothesis_id, version_id, feature_set,
    rule_version, data_version, parameter_space, status, queued_at,
    hypothesis_statement, expected_mechanism, rule_spec, source_ids,
    model_used, prompt_version, creation_timestamp
"""


class QueueInvariantError(RuntimeError):
    """A request to write across the Layer 1 boundary that must not be honoured.

    Raised instead of writing a corrupt or unwanted queue row.  Callers (the
    critic, the API) are expected to let this propagate: a refused enqueue is
    information, not an error to swallow.
    """


# ---------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------


def _job(row: dict[str, Any]) -> ExperimentJob:
    """Validate one `experiment_queue_layer2` row into the public model."""
    return ExperimentJob.model_validate(row)


def _fetch_job(cur, *, experiment_id: str | None = None, research_id: str | None = None) -> ExperimentJob | None:
    if experiment_id is not None:
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM experiment_queue_layer2 WHERE experiment_id = %s",
            (experiment_id,),
        )
    else:
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM experiment_queue_layer2 WHERE research_id = %s",
            (research_id,),
        )
    row = cur.fetchone()
    return _job(row) if row else None


def _assert_queueable(cur, hypothesis_id: str) -> str:
    """The central invariant: only PASSed hypotheses may reach Layer 2.

    Two independent refusals, because either alone is defeatable.  The status
    column can be stale relative to the failure log (and vice versa), so both
    are checked:

      1. `hypotheses.status = 'rejected'` -- the critic already killed it.
      2. a `failures` row at stage 'critic' -- a recorded critic FAIL, even if
         the status column was never advanced (a crash between the two writes,
         or a direct insert into `failures` by another tool).

    There is no third path in: this function is the only way a row reaches
    `experiment_queue`, so anything it refuses provably never reached Layer 2.
    """
    cur.execute("SELECT status FROM hypotheses WHERE hypothesis_id = %s", (hypothesis_id,))
    row = cur.fetchone()
    if row is None:
        raise QueueInvariantError(f"unknown hypothesis {hypothesis_id!r}")
    status = str(row["status"])
    if status == "rejected":
        raise QueueInvariantError(
            f"hypothesis {hypothesis_id} is rejected by the Research Critic; "
            "a rejected hypothesis must never be queued"
        )

    cur.execute(
        "SELECT failure_id, failure_type FROM failures "
        "WHERE hypothesis_id = %s AND stage = 'critic' LIMIT 1",
        (hypothesis_id,),
    )
    failure = cur.fetchone()
    if failure is not None:
        raise QueueInvariantError(
            f"hypothesis {hypothesis_id} has a recorded critic failure "
            f"(failure_id={failure['failure_id']}, "
            f"type={failure['failure_type']}); only PASSed hypotheses may be queued"
        )
    return status


def _assert_provenance_agrees(
    cur, hypothesis_id: str, version_id: int, research_id: str, rule_version: str
) -> None:
    """Check that the three identities being joined up actually refer to one thing.

    `experiment_queue` stores hypothesis_id, version_id and research_id as three
    independent foreign keys, and the Layer 2 view inner-joins all three.  A
    queue row mixing a hypothesis with another hypothesis's spec revision would
    surface to Layer 2 as a coherent-looking job that tests the wrong thing, and
    no constraint in the schema would catch it.  So the agreement is checked here
    rather than trusted.
    """
    cur.execute(
        "SELECT hypothesis_id FROM hypothesis_versions WHERE version_id = %s", (version_id,)
    )
    version_row = cur.fetchone()
    if version_row is None:
        raise QueueInvariantError(f"unknown hypothesis version {version_id!r}")
    if version_row["hypothesis_id"] != hypothesis_id:
        raise QueueInvariantError(
            f"version {version_id} belongs to hypothesis "
            f"{version_row['hypothesis_id']!r}, not {hypothesis_id!r}"
        )

    cur.execute(
        "SELECT hypothesis_id, rule_version FROM research_identity WHERE research_id = %s",
        (research_id,),
    )
    identity_row = cur.fetchone()
    if identity_row is None:
        raise QueueInvariantError(f"unknown research identity {research_id!r}")
    if identity_row["hypothesis_id"] != hypothesis_id:
        raise QueueInvariantError(
            f"research identity {research_id} belongs to hypothesis "
            f"{identity_row['hypothesis_id']!r}, not {hypothesis_id!r}"
        )
    if identity_row["rule_version"] != rule_version:
        raise QueueInvariantError(
            f"research identity {research_id} pins rule_version "
            f"{identity_row['rule_version']!r}, not {rule_version!r}"
        )


# ---------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------


def enqueue(
    hypothesis_id: str,
    version_id: int,
    research_id: str,
    feature_set: list[str],
    rule_version: str,
    data_version: str | None = None,
    parameter_space: dict | None = None,
) -> ExperimentJob:
    """Admit a critic-approved hypothesis to the Layer 2 queue.

    Idempotent on `research_id`: the same provenance tuple enqueued twice
    returns the job that already exists rather than raising, because
    `experiment_queue.research_id` is UNIQUE and the same hypothesis formalized
    to the same spec legitimately produces the same research_id.  A re-run of
    the pipeline must be safe, and refusing the second write would make the
    pipeline non-replayable for no gain.

    `data_version` defaults to `PENDING_DATA_VERSION`; `parameter_space` to `{}`
    ("run the rule exactly as written").
    """
    features = [str(f) for f in (feature_set or [])]
    if not features:
        raise QueueInvariantError(
            "feature_set must name at least one concrete variable for Layer 2 to compute"
        )
    rule = str(rule_version).strip()
    if not rule:
        raise QueueInvariantError("rule_version must be a non-empty identifier")
    version = int(version_id)
    data = data_version or PENDING_DATA_VERSION
    space: dict[str, Any] = dict(parameter_space or {})

    with db.tx() as cur:
        _assert_queueable(cur, hypothesis_id)
        _assert_provenance_agrees(cur, hypothesis_id, version, research_id, rule)

        existing = _fetch_job(cur, research_id=research_id)
        if existing is not None:
            return existing

        cur.execute(
            "UPDATE hypotheses SET status = 'queued', updated_at = now() "
            "WHERE hypothesis_id = %s",
            (hypothesis_id,),
        )
        cur.execute(
            """
            INSERT INTO experiment_queue (
                experiment_id, research_id, hypothesis_id, version_id,
                feature_set, rule_version, data_version, parameter_space
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING experiment_id
            """,
            (
                ids.next_id(cur, "EXP"),
                research_id,
                hypothesis_id,
                version,
                features,
                rule,
                data,
                db.Jsonb(space),
            ),
        )
        experiment_id = cur.fetchone()["experiment_id"]
        job = _fetch_job(cur, experiment_id=experiment_id)

    assert job is not None  # just inserted, inside the same transaction
    return job


def list_queue(status: str | None = None, limit: int = 100) -> list[ExperimentJob]:
    """Read the queue through the Layer 2 view.

    The view is the hand-off surface: it joins the rule spec, the claim, and
    the full provenance tuple into one row, so Layer 2 needs no second query
    and no knowledge of Layer 1's table layout.
    """
    capped = max(1, min(int(limit), 1000))
    with db.tx() as cur:
        if status is None:
            cur.execute(
                f"SELECT {_JOB_COLUMNS} FROM experiment_queue_layer2 "
                "ORDER BY queued_at, experiment_id LIMIT %s",
                (capped,),
            )
        else:
            cur.execute(
                f"SELECT {_JOB_COLUMNS} FROM experiment_queue_layer2 "
                "WHERE status = %s::experiment_status "
                "ORDER BY queued_at, experiment_id LIMIT %s",
                (status, capped),
            )
        return [_job(row) for row in cur.fetchall()]


def record_failure(
    hypothesis_id: str,
    reason: str,
    failure_type: str,
    stage: str = "critic",
    reported_by: str = "layer1.research_critic",
) -> FailureRecord:
    """Record a negative result.  A failure is knowledge, not an error.

    `failure_type` is validated against the schema enum here rather than left
    to Postgres, so the error names the valid values instead of surfacing a
    CHECK/cast failure with no context.
    """
    if failure_type not in _VALID_FAILURE_TYPES:
        raise ValueError(
            f"unknown failure_type {failure_type!r}; valid values are "
            f"{sorted(_VALID_FAILURE_TYPES)}"
        )
    if stage not in _VALID_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; valid values are {sorted(_VALID_STAGES)}"
        )
    text = (reason or "").strip()
    if not text:
        raise ValueError(
            "a failure needs a reason: 'did not work' is not a reason, the "
            "specific defect is"
        )

    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO failures (hypothesis_id, reason, failure_type, stage, reported_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING failure_id, hypothesis_id, reason, failure_type, stage,
                      reported_by, created_at
            """,
            (hypothesis_id, text, failure_type, stage, reported_by),
        )
        row = cur.fetchone()
    return FailureRecord.model_validate(row)


def list_failures(failure_type: str | None = None, limit: int = 100) -> list[FailureRecord]:
    """Read recorded failures, newest first.

    The critic reads this table before judging anything new, which is what
    makes the second generation of hypotheses stricter than the first.
    """
    capped = max(1, min(int(limit), 1000))
    columns = "failure_id, hypothesis_id, reason, failure_type, stage, reported_by, created_at"
    with db.tx() as cur:
        if failure_type is None:
            cur.execute(
                f"SELECT {columns} FROM failures ORDER BY created_at DESC, failure_id DESC "
                "LIMIT %s",
                (capped,),
            )
        else:
            if failure_type not in _VALID_FAILURE_TYPES:
                raise ValueError(
                    f"unknown failure_type {failure_type!r}; valid values are "
                    f"{sorted(_VALID_FAILURE_TYPES)}"
                )
            cur.execute(
                f"SELECT {columns} FROM failures WHERE failure_type = %s::failure_type "
                "ORDER BY created_at DESC, failure_id DESC LIMIT %s",
                (failure_type, capped),
            )
        return [FailureRecord.model_validate(row) for row in cur.fetchall()]
