"""Tests for the Layer 2 Result Analyst.

The analyst's value is entirely in what it REFUSES, so most of these tests
are about refusals rather than about the happy path:

* a spectacular return on an unfair test is ``inconclusive``, not ``support``;
* a profitable run whose mechanism did not hold is ``revise``, never
  ``support`` -- this is the KTD-Fin attribution case, and it is the reason
  Layer 1 stores ``expected_mechanism``;
* an injected judge may re-word a verdict but cannot change its kind;
* a ``refute`` writes negative knowledge into Layer 1, and a ``support`` does
  not;
* ``test_valid = False`` with no stated reason is refused twice over: by the
  model and by the database CHECK that outlives it;
* an unjustified trace edge, and a self-loop, are both refused.

Fixtures are built directly against the schema -- source -> note ->
hypothesis -> version -> identity -> queue -> run -> result -- rather than
through Layer 1's pipeline, because these tests are about what the analyst does
with a *recorded* run and must not depend on any other module's behaviour to
get there.  Every id carries the ``TST2_`` prefix plus a per-run suffix, and
each test deletes what it inserted, children first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from layer1 import db
from layer2 import analyst
from layer2.analyst import (
    MIN_DISCRIMINATING_TRADES,
    AnalystError,
    TraceLinkError,
    UnknownRunError,
    Verdict,
    VerdictMutationError,
    analyse,
    knowledge_for_next_generation,
    latest_verdict,
    link_run,
    list_verdicts,
    record_verdict,
)

#: A cost model that satisfies the analyst's "was a cost declared" check.
_COST_MODEL = {"commission_bps": 4.0, "spread_model": "quoted", "slippage_bps": 1.0}

#: A result payload that is a fair test of a losing hypothesis.
_LOSING = {
    "gross_return": -0.02,
    "net_return": -0.031,
    "trade_count": 120,
    "mechanism_consistent": False,
}


def _rid(kind: str) -> str:
    return f"TST2_{kind}_{uuid.uuid4().hex[:10]}"


@dataclass
class Fixture:
    """Identifiers for one full Layer 1 -> Layer 2 chain."""

    source_id: str
    mechanism_id: str
    hypothesis_id: str
    research_id: str
    experiment_id: str
    version_id: int
    run_id: str = ""
    extra_hypothesis_ids: list[str] = field(default_factory=list)
    extra_run_ids: list[str] = field(default_factory=list)

    def delete_all(self) -> None:
        """Delete everything this fixture created, children before parents."""
        hypotheses = [self.hypothesis_id, *self.extra_hypothesis_ids]
        runs = [r for r in (self.run_id, *self.extra_run_ids) if r]
        with db.tx() as cur:
            # Runs and everything hanging off them, for every run this fixture
            # made.  A teardown that forgets one leaks it into the next test
            # and into other files sharing this database.
            cur.execute(
                "DELETE FROM experiment_results WHERE run_id = ANY(%s)", (runs,)
            )
            cur.execute(
                "DELETE FROM experiment_artifacts WHERE run_id = ANY(%s)", (runs,)
            )
            cur.execute(
                "DELETE FROM analyst_verdicts WHERE run_id = ANY(%s)", (runs,)
            )
            cur.execute(
                "DELETE FROM experiment_trace WHERE parent_run_id = ANY(%s) "
                "OR child_run_id = ANY(%s)",
                (runs, runs),
            )
            cur.execute("DELETE FROM experiment_runs WHERE run_id = ANY(%s)", (runs,))
            cur.execute(
                "DELETE FROM experiment_queue WHERE experiment_id = %s",
                (self.experiment_id,),
            )
            cur.execute(
                "DELETE FROM research_identity WHERE research_id = %s", (self.research_id,)
            )
            cur.execute(
                "DELETE FROM hypothesis_versions WHERE hypothesis_id = ANY(%s)",
                (hypotheses,),
            )
            for hid in hypotheses:
                cur.execute("DELETE FROM failures WHERE hypothesis_id = %s", (hid,))
                cur.execute("DELETE FROM integrity_audits WHERE hypothesis_id = %s", (hid,))
                cur.execute(
                    "DELETE FROM research_generations WHERE parent_hypothesis_id = %s",
                    (hid,),
                )
                cur.execute("DELETE FROM hypotheses WHERE hypothesis_id = %s", (hid,))
            cur.execute(
                "DELETE FROM research_notes WHERE mechanism_id = %s", (self.mechanism_id,)
            )
            cur.execute("DELETE FROM sources WHERE source_id = %s", (self.source_id,))


def _build_chain() -> Fixture:
    """Insert one complete, queueable hypothesis.  No run yet."""
    fx = Fixture(
        source_id=_rid("SRC"),
        mechanism_id=_rid("MCH"),
        hypothesis_id=_rid("HYP"),
        research_id=_rid("RES"),
        experiment_id=_rid("EXP"),
        version_id=0,
    )
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO sources (source_id, source_type, title, content, "
            "content_sha256, date, tier) "
            "VALUES (%s, 'note', 'Analyst test evidence', 'Order imbalance reverts.', "
            "%s, now(), '3')",
            (fx.source_id, uuid.uuid4().hex),
        )
        cur.execute(
            "INSERT INTO research_notes (mechanism_id, source_id, mechanism_name, "
            "observation, mechanism_description, candidate_variables) "
            "VALUES (%s, %s, 'Liquidity replenishment', 'imbalance reverts', %s, "
            "'{order_imbalance}')",
            (
                fx.mechanism_id,
                fx.source_id,
                "passive makers absorb aggressive flow and price relaxes once it is "
                "absorbed",
            ),
        )
        cur.execute(
            "INSERT INTO hypotheses (hypothesis_id, mechanism_id, statement, "
            "expected_mechanism, candidate_variables, status) "
            "VALUES (%s, %s, 'Order imbalance predicts subsequent reversal', %s, "
            "'{order_imbalance}', 'queued')",
            (
                fx.hypothesis_id,
                fx.mechanism_id,
                "passive makers absorb aggressive flow and price relaxes once it is "
                "absorbed",
            ),
        )
        cur.execute(
            "INSERT INTO hypothesis_versions (hypothesis_id, yaml_spec, spec_sha256, "
            "model_used, prompt_version) VALUES (%s, %s::jsonb, %s, %s, %s) "
            "RETURNING version_id",
            (
                fx.hypothesis_id,
                '{"features": ["order_imbalance"], "hypothesis": {"null": "no reversal"}}',
                uuid.uuid4().hex,
                "test-model",
                "test-v1",
            ),
        )
        fx.version_id = cur.fetchone()["version_id"]
        cur.execute(
            "INSERT INTO research_identity (research_id, hypothesis_id, source_ids, "
            "feature_set, rule_version, data_version, model_used, prompt_version) "
            "VALUES (%s, %s, %s, '{order_imbalance}', %s, %s, %s, %s)",
            (
                fx.research_id,
                fx.hypothesis_id,
                [fx.source_id],
                uuid.uuid4().hex[:12],
                "binance-perp-1m-v1",
                "test-model",
                "test-v1",
            ),
        )
        cur.execute(
            "INSERT INTO experiment_queue (experiment_id, research_id, hypothesis_id, "
            "version_id, feature_set, rule_version, data_version) "
            "VALUES (%s, %s, %s, %s, '{order_imbalance}', %s, %s)",
            (
                fx.experiment_id,
                fx.research_id,
                fx.hypothesis_id,
                fx.version_id,
                uuid.uuid4().hex[:12],
                "binance-perp-1m-v1",
            ),
        )
    return fx


def _add_run(
    fx: Fixture,
    *,
    result: dict[str, Any] | None = None,
    outcome: str = "supported",
    cost_model: dict[str, Any] | None = None,
    dataset_version: str = "binance-perp-1m-v1",
    parameters: dict[str, Any] | None = None,
    run_failure_type: str | None = None,
    run_failure_reason: str | None = None,
) -> Fixture:
    """Attach a completed run carrying `result` to an existing chain."""
    fx.run_id = _rid("RUN")
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO experiment_runs (run_id, experiment_id, hypothesis_id, "
            "research_id, dataset, dataset_version, timeframe, execution_model, "
            "cost_model, parameters, backend, status, outcome, failure_type, "
            "failure_reason, code_hash, config_hash, spec_hash) "
            "VALUES (%s, %s, %s, %s, 'binance-perp', %s, '1m', %s::jsonb, %s::jsonb, "
            "%s::jsonb, 'stub', 'completed', %s::result_outcome, %s, %s, %s, %s, %s)",
            (
                fx.run_id,
                fx.experiment_id,
                fx.hypothesis_id,
                fx.research_id,
                dataset_version,
                db.Jsonb({"fill": "next_bar_open"}),
                db.Jsonb(_COST_MODEL if cost_model is None else cost_model),
                db.Jsonb(parameters or {}),
                outcome,
                run_failure_type,
                run_failure_reason,
                uuid.uuid4().hex[:12],
                uuid.uuid4().hex[:12],
                uuid.uuid4().hex[:12],
            ),
        )
        if result is not None:
            columns = ", ".join(result)
            placeholders = ", ".join(["%s"] * len(result))
            cur.execute(
                f"INSERT INTO experiment_results (run_id, {columns}) "
                f"VALUES (%s, {placeholders})",
                (fx.run_id, *result.values()),
            )
    return fx


def _spawn_hypothesis(fx: Fixture) -> str:
    """Create a real second hypothesis, for the spawn-linkage check."""
    hid = _rid("SPAWN")
    fx.extra_hypothesis_ids.append(hid)
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO hypotheses (hypothesis_id, mechanism_id, statement, "
            "expected_mechanism, candidate_variables, status) "
            "VALUES (%s, %s, 'Follow-up: imbalance reverts over a longer horizon', %s, "
            "'{order_imbalance}', 'proposed')",
            (hid, fx.mechanism_id, "absorption takes longer than one bar to complete"),
        )
    return hid


@pytest.fixture
def chain() -> Fixture:
    fx = _build_chain()
    try:
        yield fx
    finally:
        fx.delete_all()


# ---------------------------------------------------------------------
# The mapping: a loss is a refutation, and it writes negative knowledge
# ---------------------------------------------------------------------


def test_negative_net_return_is_refuted_and_writes_negative_knowledge(chain):
    """The loop closes: a refuted run becomes a Layer 1 failure row."""
    _add_run(chain, result=_LOSING, outcome="refuted")

    verdict = analyse(chain.run_id)
    assert verdict.kind == "refute"
    assert verdict.test_valid is True
    assert verdict.invalidity_reason is None

    record_verdict(verdict)

    with db.tx() as cur:
        cur.execute(
            "SELECT stage, failure_type, reason, reported_by FROM failures "
            "WHERE hypothesis_id = %s",
            (chain.hypothesis_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "layer2_backtest"
    assert row["failure_type"] in analyst.REFUTE_FAILURE_TYPES
    # "It didn't work" is not a reason: the row must carry the measurement.
    assert "-0.031" in row["reason"]
    assert row["reported_by"] == "layer2.result_analyst"


def test_edge_that_vanishes_under_costs_is_recorded_as_not_measurable(chain):
    """A gross-only edge and a nonexistent edge are different failures."""
    _add_run(
        chain,
        result={
            "gross_return": 0.14,
            "net_return": -0.02,
            "trade_count": 200,
            "mechanism_consistent": False,
        },
        outcome="refuted",
    )

    record_verdict(analyse(chain.run_id))

    with db.tx() as cur:
        cur.execute(
            "SELECT failure_type FROM failures WHERE hypothesis_id = %s",
            (chain.hypothesis_id,),
        )
        assert cur.fetchone()["failure_type"] == "not_measurable"


# ---------------------------------------------------------------------
# The attribution case: made money for the wrong reason
# ---------------------------------------------------------------------


def test_profitable_but_mechanism_inconsistent_is_revise_not_support(chain):
    """The KTD-Fin case, and the reason `expected_mechanism` is stored."""
    _add_run(
        chain,
        result={
            "gross_return": 0.31,
            "net_return": 0.22,
            "trade_count": 400,
            "market_exposure": 0.94,
            "residual_alpha": 0.01,
            "mechanism_consistent": False,
        },
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "revise"
    assert verdict.kind != "support"
    # The verdict must say the mechanism, not the return, is what failed.
    assert "mechanism" in verdict.mechanism_verdict.lower()
    assert "not support" in verdict.reasoning.lower() or "revised" in verdict.reasoning.lower()


def test_revise_writes_no_failure_because_the_idea_is_not_dead(chain):
    """A revision is negative knowledge about a *mechanism*, not a dead idea."""
    _add_run(
        chain,
        result={
            "gross_return": 0.31,
            "net_return": 0.22,
            "trade_count": 400,
            "mechanism_consistent": False,
        },
    )

    record_verdict(analyse(chain.run_id))

    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM failures WHERE hypothesis_id = %s",
            (chain.hypothesis_id,),
        )
        assert cur.fetchone()["n"] == 0


def test_profitable_and_mechanism_consistent_is_support_and_writes_no_failure(chain):
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.041,
            "trade_count": 250,
            "residual_alpha": 0.033,
            "mechanism_consistent": True,
        },
    )

    verdict = analyse(chain.run_id)
    assert verdict.kind == "support"

    record_verdict(verdict)

    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM failures WHERE hypothesis_id = %s",
            (chain.hypothesis_id,),
        )
        assert cur.fetchone()["n"] == 0


def test_profitable_but_unattributed_is_inconclusive_never_support(chain):
    """A return nobody can attribute cannot support a claim about a mechanism."""
    _add_run(
        chain,
        result={"gross_return": 0.09, "net_return": 0.041, "trade_count": 250},
    )

    verdict = analyse(chain.run_id)
    assert verdict.kind == "inconclusive"
    assert verdict.test_valid is False
    assert "attribution" in verdict.invalidity_reason


# ---------------------------------------------------------------------
# Validity first: an unfair test decides nothing, however spectacular
# ---------------------------------------------------------------------


def test_spectacular_return_on_zero_trades_is_inconclusive(chain):
    """The headline number cannot rescue a test that measured nothing."""
    _add_run(
        chain,
        result={
            "gross_return": 41.0,
            "net_return": 38.5,
            "trade_count": 0,
            "mechanism_consistent": True,
        },
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "inconclusive"
    assert verdict.test_valid is False
    assert "no trades" in verdict.invalidity_reason
    assert "could not discriminate" in verdict.reasoning


def test_return_with_no_cost_model_is_inconclusive(chain):
    """A 'net' number from a run that never said what it paid is a gross number."""
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.088,
            "trade_count": 300,
            "mechanism_consistent": True,
        },
        cost_model={},
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "inconclusive"
    assert verdict.test_valid is False
    assert "cost model" in verdict.invalidity_reason


def test_unresolved_dataset_version_is_inconclusive(chain):
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.05,
            "trade_count": 300,
            "mechanism_consistent": True,
        },
        dataset_version="latest",
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "inconclusive"
    assert verdict.test_valid is False
    assert "dataset_version" in verdict.invalidity_reason


def test_uncorrected_p_value_from_a_sweep_is_inconclusive(chain):
    """The best of N uncorrected is the multiple-testing defect, not a finding."""
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.05,
            "trade_count": 300,
            "p_value": 0.004,
            "multiple_testing_adjusted": False,
            "mechanism_consistent": True,
        },
        parameters={"lookback": [10, 20, 40, 80]},
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "inconclusive"
    assert verdict.test_valid is False
    assert "multiple variants" in verdict.invalidity_reason


def test_corrected_p_value_from_the_same_sweep_is_judged_on_its_numbers(chain):
    """The correction is what earns the right to be judged, not immunity."""
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.05,
            "trade_count": 300,
            "p_value": 0.004,
            "multiple_testing_adjusted": True,
            "mechanism_consistent": True,
        },
        parameters={"lookback": [10, 20, 40, 80]},
    )

    assert analyse(chain.run_id).kind == "support"


def test_too_few_trades_is_inconclusive_but_the_test_was_fair(chain):
    """Too small a sample says the test was blind, not that the idea is false."""
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.05,
            "trade_count": MIN_DISCRIMINATING_TRADES - 1,
            "mechanism_consistent": True,
        },
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "inconclusive"
    assert verdict.test_valid is True
    assert verdict.invalidity_reason is None


def test_p_value_that_does_not_survive_refutes(chain):
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.05,
            "trade_count": 300,
            "p_value": 0.31,
            "mechanism_consistent": True,
        },
    )

    verdict = analyse(chain.run_id)

    assert verdict.kind == "refute"
    assert "significance" in verdict.reasoning.lower()


# ---------------------------------------------------------------------
# The verdict belongs to code, not to a persuasive prompt
# ---------------------------------------------------------------------


def test_judge_may_rewrite_prose_but_not_the_kind(chain):
    _add_run(chain, result=_LOSING, outcome="refuted")
    deterministic = analyse(chain.run_id)

    reworded = analyse(
        chain.run_id, judge=lambda v: v.model_copy(update={"reasoning": "It lost money."})
    )

    assert reworded.kind == deterministic.kind == "refute"
    assert reworded.reasoning == "It lost money."
    assert reworded.test_valid is deterministic.test_valid


@pytest.mark.parametrize(
    "field,value",
    [("kind", "support"), ("kind", "inconclusive")],
)
def test_judge_cannot_upgrade_a_refutation(chain, field, value):
    """A critic that can be talked into a verdict by a prompt is not a critic."""
    _add_run(chain, result=_LOSING, outcome="refuted")

    with pytest.raises(VerdictMutationError) as exc:
        analyse(chain.run_id, judge=lambda v: v.model_copy(update={field: value}))

    assert field in str(exc.value)


def test_judge_cannot_redeclare_an_unfair_test_a_fair_one(chain):
    """Laundering a methodological flaw into evidence is the exact failure."""
    _add_run(
        chain,
        result={
            "gross_return": 41.0,
            "net_return": 38.5,
            "trade_count": 0,
            "mechanism_consistent": True,
        },
    )

    with pytest.raises(VerdictMutationError, match="test_valid"):
        analyse(chain.run_id, judge=lambda v: v.model_copy(update={"test_valid": True}))


def test_judge_cannot_erase_an_invalidity_reason(chain):
    """A defect with no stated cause is not a defect anyone can act on."""
    _add_run(chain, result={"trade_count": 0, "mechanism_consistent": True})

    with pytest.raises(VerdictMutationError, match="invalidity_reason"):
        analyse(
            chain.run_id,
            judge=lambda v: v.model_copy(update={"invalidity_reason": "never mind"}),
        )


def test_the_same_run_always_yields_the_same_verdict(chain):
    """Determinism is what makes the verdict a fact rather than an impression."""
    _add_run(
        chain,
        result={
            "gross_return": 0.09,
            "net_return": 0.05,
            "trade_count": 300,
            "mechanism_consistent": False,
        },
    )
    first = analyse(chain.run_id)
    second = analyse(chain.run_id)
    assert first == second


# ---------------------------------------------------------------------
# An invalid verdict must explain itself -- twice over
# ---------------------------------------------------------------------


def test_invalid_verdict_without_a_reason_is_refused_by_the_model():
    with pytest.raises(ValidationError, match="invalidity_reason"):
        Verdict(
            run_id="TST2_RUN_x",
            hypothesis_id="TST2_HYP_x",
            kind="inconclusive",
            reasoning="something",
            test_valid=False,
        )


def test_database_check_refuses_an_unexplained_invalid_verdict(chain):
    """The model guard is not enough: the CHECK outlives every caller."""
    _add_run(chain, result=_LOSING, outcome="refuted")

    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO analyst_verdicts (verdict_id, run_id, hypothesis_id, "
                "kind, reasoning, test_valid, invalidity_reason) "
                "VALUES (%s, %s, %s, 'inconclusive', 'no reason given', FALSE, NULL)",
                (_rid("VER"), chain.run_id, chain.hypothesis_id),
            )


# ---------------------------------------------------------------------
# Reading verdicts back
# ---------------------------------------------------------------------


def test_recorded_verdict_round_trips_and_filters_by_kind(chain):
    _add_run(chain, result=_LOSING, outcome="refuted")
    verdict = analyse(chain.run_id)
    verdict_id = record_verdict(verdict)
    assert verdict_id.startswith("VER_")

    stored = latest_verdict(chain.hypothesis_id)
    assert stored == verdict

    # Every read is scoped to this test's own hypothesis. An unscoped read
    # would make the assertion depend on what every other test in the suite
    # happened to record, which is how a passing test stops testing anything.
    only = list_verdicts(hypothesis_id=chain.hypothesis_id)
    assert [v.kind for v in only] == ["refute"]

    # The kind filter has to discriminate, not merely pass rows through.
    assert [v.kind for v in list_verdicts(hypothesis_id=chain.hypothesis_id, kind="refute")] == [
        "refute"
    ]
    assert list_verdicts(hypothesis_id=chain.hypothesis_id, kind="support") == []

def test_latest_verdict_returns_the_newest_judgement_on_a_hypothesis(chain):
    """A hypothesis can be judged more than once, and the newest must win."""
    _add_run(chain, result=_LOSING, outcome="refuted")
    first = record_verdict(analyse(chain.run_id))

    second_run = _second_run(chain)
    second = record_verdict(analyse(second_run))
    assert second != first

    newest = latest_verdict(chain.hypothesis_id)
    assert newest.run_id == second_run
    assert newest.kind == "inconclusive"
    assert len(list_verdicts(hypothesis_id=chain.hypothesis_id)) == 2


def test_latest_verdict_is_none_before_anything_is_recorded(chain):
    assert latest_verdict(chain.hypothesis_id) is None


def test_recording_the_same_kind_twice_is_refused(chain):
    """Otherwise a failure count measures how often the analyst was called."""
    _add_run(chain, result=_LOSING, outcome="refuted")
    verdict = analyse(chain.run_id)
    record_verdict(verdict)

    with pytest.raises(AnalystError, match="already has a 'refute' verdict"):
        record_verdict(verdict)

    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM failures WHERE hypothesis_id = %s",
            (chain.hypothesis_id,),
        )
        assert cur.fetchone()["n"] == 1


def test_a_verdict_cannot_be_attached_to_another_hypotheses_run(chain):
    _add_run(chain, result=_LOSING, outcome="refuted")
    verdict = analyse(chain.run_id).model_copy(update={"hypothesis_id": _rid("OTHER")})

    with pytest.raises(AnalystError, match="belongs to hypothesis"):
        record_verdict(verdict)


def test_spawned_hypothesis_id_must_be_a_hypothesis_that_exists(chain):
    """A fabricated follow-up id is the most convenient way to invent progress."""
    _add_run(
        chain,
        result={
            "gross_return": 0.31,
            "net_return": 0.22,
            "trade_count": 400,
            "mechanism_consistent": False,
        },
    )
    verdict = analyse(chain.run_id).model_copy(update={"spawned_hypothesis_id": _rid("GHOST")})

    with pytest.raises(UnknownRunError, match="does not exist"):
        record_verdict(verdict)


def test_a_revise_verdict_may_name_a_real_follow_up_hypothesis(chain):
    """The spawn is recorded only because the hypothesis was really created."""
    _add_run(
        chain,
        result={
            "gross_return": 0.31,
            "net_return": 0.22,
            "trade_count": 400,
            "mechanism_consistent": False,
        },
    )
    spawned = _spawn_hypothesis(chain)

    verdict = analyse(chain.run_id).model_copy(update={"spawned_hypothesis_id": spawned})
    record_verdict(verdict)

    stored = latest_verdict(chain.hypothesis_id)
    assert stored.spawned_hypothesis_id == spawned


def test_verdict_for_an_unknown_run_is_refused():
    with pytest.raises(UnknownRunError):
        analyse("TST2_RUN_does_not_exist")


# ---------------------------------------------------------------------
# Feeding the next generation
# ---------------------------------------------------------------------


def test_next_generation_knowledge_surfaces_a_recorded_failure(chain):
    """The negative knowledge a refute wrote must be readable by the generator."""
    _add_run(chain, result=_LOSING, outcome="refuted")
    record_verdict(analyse(chain.run_id))

    knowledge = knowledge_for_next_generation()

    mine = [f for f in knowledge["failures"] if f["hypothesis_id"] == chain.hypothesis_id]
    assert len(mine) == 1
    assert mine[0]["stage"] == "layer2_backtest"
    assert mine[0]["failure_type"] in analyst.REFUTE_FAILURE_TYPES
    assert mine[0]["mechanism_id"] == chain.mechanism_id

    rollup = [m for m in knowledge["refuted_mechanisms"] if m["mechanism_id"] == chain.mechanism_id]
    assert len(rollup) == 1
    assert rollup[0]["refutations"] == 1
    assert rollup[0]["failure_types"] == ["not_falsifiable"]
    assert "-0.031" in rollup[0]["latest_reason"]
    assert knowledge["counts"]["failures"] >= 1


def test_next_generation_knowledge_lists_a_recorded_contradiction(chain):
    """A source conflict is a lead, and the generator has to be able to see it."""
    claim_ids = [_rid("CLM"), _rid("CLM")]
    with db.tx() as cur:
        for cid in claim_ids:
            cur.execute(
                "INSERT INTO claims (claim_id, source_id, statement, claim_type) "
                "VALUES (%s, %s, 'imbalance reverts', 'author_claim')",
                (cid, chain.source_id),
            )
        cur.execute(
            "INSERT INTO claim_contradictions (contradiction_id, claim_a, claim_b, "
            "possible_reasons, resolution_status) "
            "VALUES (%s, %s, %s, ARRAY['regime']::conflict_reason[], 'unresolved')",
            (_rid("CTR"), claim_ids[0], claim_ids[1]),
        )
    try:
        knowledge = knowledge_for_next_generation()
        mine = [
            c for c in knowledge["contradictions"] if c["claim_a_id"] == claim_ids[0]
        ]
        assert len(mine) == 1
        assert mine[0]["possible_reasons"] == ["regime"]
        assert mine[0]["claim_a_statement"] == "imbalance reverts"
        assert mine[0]["resolution_status"] == "unresolved"
    finally:
        with db.tx() as cur:
            cur.execute(
                "DELETE FROM claim_contradictions WHERE claim_a = %s", (claim_ids[0],)
            )
            for cid in claim_ids:
                cur.execute("DELETE FROM claims WHERE claim_id = %s", (cid,))


# ---------------------------------------------------------------------
# The trace DAG
# ---------------------------------------------------------------------


def _second_run(fx: Fixture) -> str:
    """A second run on the same hypothesis, for the trace and recency tests."""
    run_id = _rid("RUN2")
    fx.extra_run_ids.append(run_id)
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO experiment_runs (run_id, experiment_id, hypothesis_id, "
            "research_id, dataset, dataset_version, cost_model, backend, status, outcome) "
            "VALUES (%s, %s, %s, %s, 'binance-perp', 'binance-perp-1m-v1', %s::jsonb, "
            "'stub', 'completed', 'inconclusive')",
            (run_id, fx.experiment_id, fx.hypothesis_id, fx.research_id, db.Jsonb(_COST_MODEL)),
        )
    return run_id


def test_link_run_records_an_edge_with_its_reason(chain):
    _add_run(chain, result=_LOSING, outcome="refuted")
    child = _second_run(chain)

    edge_id = link_run(
        chain.run_id,
        child,
        reason="revision",
        rationale="the edge did not survive costs; widen the horizon and retry",
    )

    assert edge_id.startswith("EDGE_")
    with db.tx() as cur:
        cur.execute(
            "SELECT parent_run_id, child_run_id, reason, rationale FROM experiment_trace "
            "WHERE edge_id = %s",
            (edge_id,),
        )
        row = cur.fetchone()
    assert row["parent_run_id"] == chain.run_id
    assert row["child_run_id"] == child
    assert row["reason"] == "revision"
    assert "costs" in row["rationale"]

    # Cleanup belongs to the fixture alone.  Duplicating it inline here is how
    # the two copies drift apart and one of them silently stops deleting.


def test_link_run_requires_a_rationale(chain):
    """An unjustified edge is a rerun, not progress."""
    _add_run(chain, result=_LOSING, outcome="refuted")
    child = _second_run(chain)

    with pytest.raises(TraceLinkError, match="rationale"):
        link_run(chain.run_id, child, reason="revision", rationale="   ")



def test_link_run_rejects_a_self_loop(chain):
    """A run that justifies itself is the opposite of a lineage."""
    _add_run(chain, result=_LOSING, outcome="refuted")

    with pytest.raises(TraceLinkError, match="self-loop"):
        link_run(
            chain.run_id,
            chain.run_id,
            reason="revision",
            rationale="trying the same thing again",
        )

    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM experiment_trace WHERE parent_run_id = %s",
            (chain.run_id,),
        )
        assert cur.fetchone()["n"] == 0


def test_link_run_rejects_an_unknown_run(chain):
    _add_run(chain, result=_LOSING, outcome="refuted")

    with pytest.raises(UnknownRunError):
        link_run(
            chain.run_id,
            "TST2_RUN_absent",
            reason="revision",
            rationale="there is no such run",
        )


def test_link_run_rejects_an_unknown_reason(chain):
    _add_run(chain, result=_LOSING, outcome="refuted")
    child = _second_run(chain)

    with pytest.raises(TraceLinkError, match="unknown trace edge reason"):
        link_run(chain.run_id, child, reason="because", rationale="no")

