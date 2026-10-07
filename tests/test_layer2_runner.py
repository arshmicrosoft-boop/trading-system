"""Integration tests for the Layer 2 run lifecycle against the live database.

These exercise the WRITE path end to end, because the write path is where the
architecture's claims stop being design and start being behaviour.  In
particular the results INSERT is executed here for real: a placeholder/param
desynchronisation in `complete_run` was live for a while and no test that only
called the pure mapping helpers would ever have seen it, so the assertion
below reads `net_return` back out of the column it was written to rather than
out of the object that was passed in.

Every test inserts a uniquely named chain and deletes it again, including on
failure.  Ids carry a per-run suffix so these coexist with any other writer
against the same database.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from layer1 import db
from layer1.components.queueing import PENDING_DATA_VERSION
from layer2 import runner
from layer2.rbi_adapter import RunOutcome

__all__ = ["Chain", "chain", "make_chain", "cleanup_chain"]


def _rid(kind: str) -> str:
    return f"L2RUN_{kind}_{uuid.uuid4().hex[:10]}"


def _sha() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Chain:
    """Identifiers for one fully linked research chain."""

    source_id: str
    mechanism_id: str
    hypothesis_id: str
    version_id: int
    research_id: str
    experiment_id: str
    data_version: str = "l2-test-dataset-v1"


def cleanup_chain(chain: Chain) -> None:
    """Remove the chain, children before parents.

    Deletion order is not cosmetic: every one of these tables carries a
    foreign key, so a parent removed first raises and the transaction rolls
    back, leaving the database dirty.
    """
    with db.tx() as cur:
        cur.execute(
            "DELETE FROM experiment_results WHERE run_id IN "
            "(SELECT run_id FROM experiment_runs WHERE experiment_id = %s)",
            (chain.experiment_id,),
        )
        cur.execute(
            "DELETE FROM experiment_artifacts WHERE run_id IN "
            "(SELECT run_id FROM experiment_runs WHERE experiment_id = %s)",
            (chain.experiment_id,),
        )
        cur.execute(
            "DELETE FROM experiment_runs WHERE experiment_id = %s", (chain.experiment_id,)
        )
        cur.execute("DELETE FROM experiment_queue WHERE experiment_id = %s", (chain.experiment_id,))
        cur.execute("DELETE FROM research_identity WHERE research_id = %s", (chain.research_id,))
        cur.execute(
            "DELETE FROM hypothesis_versions WHERE hypothesis_id = %s", (chain.hypothesis_id,)
        )
        cur.execute("DELETE FROM hypotheses WHERE hypothesis_id = %s", (chain.hypothesis_id,))
        cur.execute("DELETE FROM research_notes WHERE mechanism_id = %s", (chain.mechanism_id,))
        cur.execute("DELETE FROM sources WHERE source_id = %s", (chain.source_id,))


def make_chain(data_version: str = "l2-test-dataset-v1") -> Chain:
    """Insert a source -> note -> hypothesis -> version -> identity -> queue chain."""
    source_id, mechanism_id, hypothesis_id = _rid("SRC"), _rid("MCH"), _rid("HYP")
    rule_spec = {
        "hypothesis_id": hypothesis_id,
        "name": "Runner_Spec",
        "source": "order flow imbalance",
        "market": {"asset_class": "crypto", "universe": "BTC/USDT perpetual"},
        "features": ["signed_volume_imbalance_1m"],
        "hypothesis": {
            "statement": "Persistent signed volume imbalance predicts drift",
            "signal": {"condition": "signed_volume_imbalance_1m > 2.0"},
            "null": "no forward drift conditional on imbalance",
        },
        "expected_mechanism": "makers absorb informed flow",
    }
    chain = Chain(
        source_id=source_id,
        mechanism_id=mechanism_id,
        hypothesis_id=hypothesis_id,
        version_id=0,
        research_id="R_" + uuid.uuid4().hex[:16],
        experiment_id=_rid("EXP"),
        data_version=data_version,
    )
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO sources
                (source_id, source_type, title, content, content_sha256, date, tier)
            VALUES (%s, 'note', %s, %s, %s, now(), '3')
            """,
            (chain.source_id, "Layer 2 runner test", "imbalance precedes drift", _sha()),
        )
        cur.execute(
            """
            INSERT INTO research_notes
                (mechanism_id, source_id, mechanism_name, observation,
                 mechanism_description, candidate_variables)
            VALUES (%s, %s, 'OFI absorption', 'imbalance persists briefly',
                    'Informed makers consume imbalance before it reverses',
                    '{signed_volume_imbalance_1m}')
            """,
            (chain.mechanism_id, chain.source_id),
        )
        cur.execute(
            """
            INSERT INTO hypotheses
                (hypothesis_id, mechanism_id, statement, expected_mechanism,
                 candidate_variables)
            VALUES (%s, %s,
                    'Persistent signed volume imbalance predicts drift',
                    'Informed makers consume imbalance before it reverses',
                    '{signed_volume_imbalance_1m}')
            """,
            (chain.hypothesis_id, chain.mechanism_id),
        )
        cur.execute(
            """
            INSERT INTO hypothesis_versions
                (hypothesis_id, yaml_spec, spec_sha256, model_used, prompt_version)
            VALUES (%s, %s::jsonb, %s, 'deterministic/v1', 'l1-v1')
            RETURNING version_id
            """,
            (chain.hypothesis_id, json.dumps(rule_spec), _sha()),
        )
        version_id = cur.fetchone()["version_id"]
        cur.execute(
            """
            INSERT INTO research_identity
                (research_id, hypothesis_id, source_ids, feature_set,
                 rule_version, data_version, model_used, prompt_version)
            VALUES (%s, %s, %s, '{signed_volume_imbalance_1m}',
                    'Runner_Spec_v1', %s, 'deterministic/v1', 'l1-v1')
            """,
            (chain.research_id, chain.hypothesis_id, [chain.source_id], chain.data_version),
        )
        cur.execute(
            """
            INSERT INTO experiment_queue
                (experiment_id, research_id, hypothesis_id, version_id,
                 feature_set, rule_version, data_version)
            VALUES (%s, %s, %s, %s, '{signed_volume_imbalance_1m}',
                    'Runner_Spec_v1', %s)
            RETURNING status
            """,
            (
                chain.experiment_id,
                chain.research_id,
                chain.hypothesis_id,
                version_id,
                chain.data_version,
            ),
        )
        assert cur.fetchone()["status"] == "queued"
    return Chain(**{**chain.__dict__, "version_id": version_id})


@pytest.fixture
def chain():
    """A linked chain, torn down even when the test fails."""
    built = make_chain()
    try:
        yield built
    finally:
        cleanup_chain(built)


def _row(sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    with db.tx() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ---------------------------------------------------------------------
# The results INSERT
# ---------------------------------------------------------------------


def test_complete_run_writes_metrics_to_real_columns(chain):
    """`complete_run` must land metrics in `experiment_results` COLUMNS.

    This is the regression test for a live defect: the INSERT built its
    placeholder list from a different count than its parameter tuple, so every
    call raised before writing anything.  The assertion reads the value back
    with plain SQL, deliberately ignoring the `RunRecord` the function
    returned -- a test that only inspected the return value would pass again
    the moment the INSERT and the model disagree.
    """
    run = runner.start_run(chain.experiment_id, backend="stub", dataset="binance-perps")

    runner.complete_run(
        run.run_id,
        RunOutcome(
            status="completed",
            outcome="refuted",
            metrics={"gross_return": 0.42, "net_return": 0.11, "trade_count": 340.0},
        ),
    )

    stored = _row(
        "SELECT gross_return, net_return, trade_count, multiple_testing_adjusted,"
        " factor_exposure FROM experiment_results WHERE run_id = %s",
        (run.run_id,),
    )
    assert stored is not None
    assert stored["net_return"] == pytest.approx(0.11)
    assert stored["gross_return"] == pytest.approx(0.42)
    assert stored["trade_count"] == 340
    # Absent means "not corrected", and False is a fact the row must be able
    # to state rather than a NULL a consumer has to guess about.
    assert stored["multiple_testing_adjusted"] is False
    assert stored["factor_exposure"] == {}


def test_unmapped_metric_is_preserved_with_a_note(chain):
    """A metric with no column is moved aside and named, never dropped."""
    run = runner.start_run(chain.experiment_id)
    runner.complete_run(
        run.run_id,
        RunOutcome(
            status="completed",
            outcome="supported",
            metrics={"net_return": 0.05, "my_custom_ratio": 1.25},
        ),
    )
    stored = _row(
        "SELECT extra_metrics FROM experiment_results WHERE run_id = %s", (run.run_id,)
    )
    assert stored["extra_metrics"]["my_custom_ratio"] == pytest.approx(1.25)
    assert stored["extra_metrics"][runner.UNMAPPED_KEY] == ["my_custom_ratio"]


def test_run_report_exposes_metrics_and_hides_stdout(chain):
    """The report view is the consumer surface, and it deliberately omits stdout."""
    run = runner.start_run(chain.experiment_id)
    runner.complete_run(
        run.run_id,
        RunOutcome(
            status="completed",
            outcome="supported",
            metrics={"net_return": 0.2},
            stdout="engine chatter",
            workspace_path="/tmp/ws",
        ),
    )
    report = runner.run_report(run.run_id)
    assert report["net_return"] == pytest.approx(0.2)
    assert "stdout" not in report
    assert report["is_real_engine"] is False


# ---------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------


def test_run_moves_from_running_to_completed(chain):
    run = runner.start_run(chain.experiment_id)
    assert run.status == "running"
    done = runner.complete_run(
        run.run_id, RunOutcome(status="completed", outcome="supported", metrics={"net_return": 0.3})
    )
    assert done.status == "completed"
    assert done.outcome == "supported"
    assert done.finished_at is not None


def test_cannot_complete_a_run_twice(chain):
    run = runner.start_run(chain.experiment_id)
    outcome = RunOutcome(status="completed", outcome="supported", metrics={"net_return": 0.1})
    runner.complete_run(run.run_id, outcome)
    with pytest.raises(runner.RunStateError, match="not 'running'"):
        runner.complete_run(run.run_id, outcome)


def test_cannot_complete_an_unknown_run():
    with pytest.raises(runner.RunStateError, match="unknown run"):
        runner.complete_run("RUN_doesnotexist", RunOutcome(status="completed", outcome="supported", metrics={"net_return": 0.1}))


def test_completed_run_requires_net_return(chain):
    """gross_return alone is a marketing number, so it is refused."""
    run = runner.start_run(chain.experiment_id)
    with pytest.raises(runner.RunStateError, match="net_return"):
        runner.complete_run(
            run.run_id,
            RunOutcome(status="completed", outcome="supported", metrics={"gross_return": 0.9}),
        )


def test_completed_run_cannot_claim_outcome_errored(chain):
    run = runner.start_run(chain.experiment_id)
    with pytest.raises(runner.RunStateError, match="errored"):
        runner.complete_run(
            run.run_id, RunOutcome(status="completed", outcome="errored", metrics={"net_return": 0.1})
        )


def test_fractional_trade_count_is_refused(chain):
    """An INTEGER column given 3.5 is a broken engine, not a value to round."""
    run = runner.start_run(chain.experiment_id)
    with pytest.raises(runner.RunStateError, match="INTEGER"):
        runner.complete_run(
            run.run_id,
            RunOutcome(
                status="completed",
                outcome="supported",
                metrics={"net_return": 0.1, "trade_count": 3.5},
            ),
        )


def test_fail_run_records_outcome_and_reason(chain):
    run = runner.start_run(chain.experiment_id)
    failed = runner.fail_run(run.run_id, "engine_error", "series was empty")
    assert failed.status == "failed"
    assert failed.outcome == "errored"
    assert failed.failure_type == "engine_error"
    # A broken run has no result, and an empty row would read as a flat finding.
    assert _row("SELECT 1 FROM experiment_results WHERE run_id = %s", (run.run_id,)) is None


def test_fail_run_requires_a_reason(chain):
    run = runner.start_run(chain.experiment_id)
    with pytest.raises(runner.RunStateError, match="unauditable"):
        runner.fail_run(run.run_id, "engine_error", "   ")


def test_fail_run_requires_a_type(chain):
    run = runner.start_run(chain.experiment_id)
    with pytest.raises(runner.RunStateError, match="failure_type"):
        runner.fail_run(run.run_id, "", "something broke")


def test_cannot_fail_a_finished_run(chain):
    run = runner.start_run(chain.experiment_id)
    runner.complete_run(
        run.run_id, RunOutcome(status="completed", outcome="supported", metrics={"net_return": 0.1})
    )
    with pytest.raises(runner.RunStateError, match="has not finished"):
        runner.fail_run(run.run_id, "engine_error", "too late")


# ---------------------------------------------------------------------
# start_run guards
# ---------------------------------------------------------------------


def test_start_run_defaults_metadata_from_the_queued_job():
    built = make_chain()
    try:
        run = runner.start_run(built.experiment_id)
        # Nothing was passed, and everything recorded came from the queue.
        assert run.dataset_version == built.data_version
        assert run.universe["universe"] == "BTC/USDT perpetual"
        assert run.version_id == built.version_id
        # spec_hash is derived so the run records the spec it was authorised
        # against without the caller having to compute it.
        assert run.spec_hash
    finally:
        cleanup_chain(built)


def test_start_run_refuses_the_unresolved_dataset_sentinel():
    built = make_chain(data_version=PENDING_DATA_VERSION)
    try:
        with pytest.raises(runner.RunStateError, match=PENDING_DATA_VERSION):
            runner.start_run(built.experiment_id)
        # ...and pinning it explicitly is the resolution path Layer 2 owns.
        run = runner.start_run(built.experiment_id, dataset_version="l2-2026-01-frozen")
        assert run.dataset_version == "l2-2026-01-frozen"
    finally:
        cleanup_chain(built)


def test_start_run_refuses_an_unknown_experiment():
    with pytest.raises(runner.RunStateError, match="not in the Layer 1 queue"):
        runner.start_run(f"L2RUN_EXP_{uuid.uuid4().hex[:10]}")


def test_start_run_refuses_an_unavailable_backend(chain):
    """`moondev_rbi` is registered but is_available = false: not wired in here."""
    with pytest.raises(runner.RunStateError, match="is_available"):
        runner.start_run(chain.experiment_id, backend="moondev_rbi")


def test_start_run_refuses_a_mixed_identity_job(chain):
    """A job whose version belongs to another hypothesis must never run.

    The queue stores hypothesis_id, version_id and research_id as three
    independent foreign keys, so a row mixing two hypotheses is legal to the
    schema and would surface as a coherent job testing the wrong thing.  The
    immutability trigger has to be lifted to construct one, which is precisely
    the point: only a writer with DDL rights can produce this, and Layer 2
    refuses it rather than spending compute on the wrong answer.
    """
    other = make_chain()
    try:
        with db.tx() as cur:
            cur.execute("ALTER TABLE experiment_queue DISABLE TRIGGER experiment_queue_immutable_identity")
            cur.execute(
                "UPDATE experiment_queue SET hypothesis_id = %s WHERE experiment_id = %s",
                (other.hypothesis_id, chain.experiment_id),
            )
            cur.execute("ALTER TABLE experiment_queue ENABLE TRIGGER experiment_queue_immutable_identity")
        with pytest.raises(runner.RunStateError, match="mixes identities"):
            runner.start_run(chain.experiment_id)
    finally:
        cleanup_chain(chain)
        cleanup_chain(other)


def test_start_run_rejects_an_unknown_backend_name(chain):
    with pytest.raises(runner.RunStateError, match="not one of"):
        runner.start_run(chain.experiment_id, backend="my_private_hedge_fund")


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------


def test_list_runs_filters_by_hypothesis(chain):
    run = runner.start_run(chain.experiment_id)
    runner.complete_run(
        run.run_id, RunOutcome(status="completed", outcome="supported", metrics={"net_return": 0.4})
    )
    assert [r.run_id for r in runner.list_runs(hypothesis_id=chain.hypothesis_id)] == [run.run_id]
    assert runner.list_runs(hypothesis_id="H9999") == []
    # Scoped to this experiment: an unfiltered status read spans every run in
    # the database, including any a sibling writer has in flight.
    assert [r.run_id for r in runner.list_runs(chain.experiment_id, status="completed")] == [run.run_id]


def test_get_run_distinguishes_unmeasured_from_measured_zero(chain):
    run = runner.start_run(chain.experiment_id)
    assert runner.get_run(run.run_id).net_return is None
    runner.complete_run(
        run.run_id, RunOutcome(status="completed", outcome="supported", metrics={"net_return": 0.0})
    )
    assert runner.get_run(run.run_id).net_return == pytest.approx(0.0)
    assert runner.get_run("RUN_nope") is None