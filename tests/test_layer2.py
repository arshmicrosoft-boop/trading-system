"""The closed loop, end to end, through the real Layer 1 code.

    source -> evidence -> mechanism -> hypothesis -> RuleSpec
          -> critique -> integrity gate -> experiment queue
          -> start_run -> complete_run -> result row
          -> analyst verdict -> Layer 1 `failures`

This is an INTEGRATION test. It drives `layer1.pipeline.run_pipeline` with the
canned agent responses, so every stage between the source and the queue is the
production implementation rather than a stand-in. What is stubbed is the
research model's judgement, which is the only thing that cannot be
reproduced; what is stubbed at Layer 2 is the *engine*, and that stub is
itself the subject of several tests here.

Nothing in this file asserts on an unfiltered `list_runs()` or
`list_queue()` across the whole database: the database is live and holds real
pipeline data, so every assertion is scoped to an identifier this module
created.

Cleanup: every run registers its identifiers, and a session finalizer deletes
them children-first. The final test reads back to prove the tables are clean.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any

# Before anything imports `layer1.config`: the settings object is cached and
# `.env` names a hosted provider. The canned answers are installed regardless,
# but the recorded model identity should say what actually ran.
os.environ["L1_LLM_MODE"] = "deterministic"

import psycopg
import pytest

from layer1 import db, integrity, llm
from layer1.components import critic, queueing
from layer1.components.queueing import PENDING_DATA_VERSION
from layer1.config import get_settings
from layer1.contracts import RuleSpec, SourceIn
from layer1.db import apply_schema
from layer1.pipeline import PipelineRun, run_pipeline
from examples import canned

from layer2 import analyst, runner
from layer2.rbi_adapter import ExperimentSpec
from layer2.runner import RunStateError
from layer2.stub_runner import StubAdapter, metric_columns


# ---------------------------------------------------------------------
# Canned agent responses
# ---------------------------------------------------------------------

#: The evidence stage. `examples.canned` covers extraction, generation,
#: formalization and critique; the pipeline's claim-extraction call falls
#: through to the "generate" answer, which carries no `claims` key, so the
#: evidence hop would record nothing. These claims give that hop something
#: real to record -- `record_model_inference` types them as `model_inference`
#: no matter what the answer claims for itself.
_EVIDENCE_RESPONSE: dict[str, Any] = {
    "claims": [
        {
            "statement": (
                "Aggressive volume arriving at the bid exceeded volume lifting "
                "the ask over the observation window."
            ),
            "claim_type": "empirical_result",
            "section_ref": "transcript 00:04:10-00:06:30",
            "evidence_strength": "moderate",
            "agent_interpretation": (
                "The imbalance is a proxy for unfilled intent rather than for "
                "momentum that has already been realised."
            ),
        },
        {
            "statement": (
                "The imbalance resolved into price movement within the same "
                "window in which the resting book was consumed."
            ),
            "claim_type": "empirical_result",
            "section_ref": "transcript 00:06:30-00:09:00",
            "evidence_strength": "weak",
            "agent_interpretation": None,
        },
    ]
}

_llm_installed = False


def _install_canned_llm() -> None:
    """Install the canned agent answers, once per process.

    `canned.install` patches `LLMClient.complete_json`; this wraps that patch
    so the pipeline's evidence-extraction call gets an answer with claims in
    it, and delegates everything else back to the canned router.
    """
    global _llm_installed
    if _llm_installed:
        return
    canned.install()
    canned_llm = llm.LLMClient.complete_json

    def complete_json(self, system: str, user: str, **kwargs: object) -> tuple[Any, str]:
        if "claim_type" in system:
            return _EVIDENCE_RESPONSE, "deterministic/v1"
        return canned_llm(self, system, user, **kwargs)

    llm.LLMClient.complete_json = complete_json  # type: ignore[method-assign]
    _llm_installed = True


def _reset_canned_counters() -> None:

    """Rewind the per-stage FIFO so every pipeline run sees the same answers.

    `canned` dispatches on call order within a stage, and its counters are
    module-global. Without this, the second pipeline run in a session would be
    critiqued against a different position in the answer list and the test would
    be asserting on an accident.
    """
    canned._counters.update(dict.fromkeys(canned._BY_STAGE, 0))


# ---------------------------------------------------------------------
# Source material
# ---------------------------------------------------------------------

_SOURCE_BODY = """
Order flow imbalance in this venue is the difference between aggressive volume
arriving at the bid and aggressive volume lifting the ask, measured over a
short window on the consolidated book. Across three sessions the arriving side
dominated roughly three times in four, and the resting side of the book was
consumed rather than replenished over the same interval.

A depth-weighted price inside the spread sits closer to the bid when bid depth
exceeds ask depth, and closer to the ask in the opposite case. The two
measures appear to describe the same imbalance in price space and volume space
respectively, so their disagreement is read here as measurement noise from
cancellations and their agreement as a quantity large enough to survive it.

The author's claim is that the imbalance carries information about intent that
has not yet been expressed as a trade, and that this information resolves into
price once the passive liquidity on the exhausted side is gone.
"""


def _source_in(tag: str) -> SourceIn:
    """A source whose content is unique per call.

    `sources` dedupes on `content_sha256`, so two tests using identical text
    would share one source row -- and the second test's cleanup would then
    delete rows belonging to the first. The tag makes the content unique so
    each run owns exactly what it created.
    """
    return SourceIn(
        source_type="youtube",
        title=f"Order flow imbalance and microprice ({tag})",
        author="Moon Dev",
        url="https://youtube.com/@moondev",
        content=f"{_SOURCE_BODY}\nSession marker: {tag}\n",
        tier=4,
    )


# ---------------------------------------------------------------------
# The experimental protocol
# ---------------------------------------------------------------------


def _complete_protocol() -> dict[str, Any]:
    """Every block concrete, per reproducibility_audit.md.

    Modelled on the protocol the integrity suite proves passes; the gate is
    checked properly there, and here it only has to be complete enough that
    the queue is reached.
    """
    return {
        "temporal_protocol": {
            "train": {"start": "2021-01-01", "end": "2022-12-31"},
            "validation": {"start": "2023-01-01", "end": "2023-06-30"},
            "test": {"start": "2023-07-01", "end": "2024-12-31"},
            "horizon": "4 hours",
            "split_type": "anchored",
            "purge_embargo_hours": 4.0,
        },
        "universe": {
            "assets": ["BTC/USDT", "ETH/USDT"],
            "inclusion_rule": "listed continuously for the whole test window",
            "survivorship_policy": "delisted_retained",
        },
        "execution": {
            "signal_time": "close of bar k",
            "execution_time": "open of bar k+1",
            "price": "mid",
            "latency_ms": 250.0,
        },
        "costs": {
            "commission_bps": 4.0,
            "spread_model": "fixed_bps",
            "spread_bps": 1.0,
            "slippage_model": "fixed_bps",
            "slippage_bps": 0.5,
            "funding": "applied",
        },
        "data": {
            "provider": "reference-vendor",
            "dataset_version": "2024-03-snapshot-01",
        },
    }


class _EveryHypothesis(dict[str, Any]):
    """A `protocols` mapping that answers for any hypothesis id.

    `run_pipeline` looks the protocol up by `hypothesis_id`, and those ids are
    allocated inside the run, so a caller that knows exactly what its protocol
    is cannot name the key beforehand. Handing the pipeline a mapping that
    supplies the same protocol to whatever hypothesis turns up is the honest
    way to say "this protocol covers every hypothesis from this source", and it
    keeps the test driving `run_pipeline` rather than re-implementing its
    stages.
    """

    def get(self, key: str, default: Any = None) -> Any:  # noqa: D102
        return _complete_protocol()


# ---------------------------------------------------------------------
# Registry: what this module created, and how to remove it
# ---------------------------------------------------------------------

#: What this session inserted, by bucket. Run-keyed tables all hang off the
#: run ids, so they are purged together rather than tracked separately -- a
#: `experiment_results` row whose run id is being deleted would otherwise
#: survive and then block the delete of its own parent.
@dataclass
class Registry:
    """Identifiers this test session inserted, grouped by what they identify."""

    runs: set[str] = field(default_factory=set)
    experiments: set[str] = field(default_factory=set)
    research: set[str] = field(default_factory=set)
    hypotheses: set[str] = field(default_factory=set)
    mechanisms: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)

    def _ordered(self) -> list[tuple[str, str, list[str]]]:
        """Child-first. Each table is deleted only if it has rows of ours.

        `experiment_runs` references `experiment_queue`, which references
        `research_identity` and `hypothesis_versions`, which reference
        `hypotheses`; `failures` and `integrity_audits` reference
        `hypotheses` too. `experiment_trace` has no `run_id` column -- it
        names `parent_run_id` and `child_run_id` -- so it matches on either.
        """
        runs, hyps = sorted(self.runs), sorted(self.hypotheses)
        return [
            ("experiment_results", "run_id", runs),
            ("experiment_artifacts", "run_id", runs),
            ("analyst_verdicts", "run_id", runs),
            ("experiment_runs", "run_id", runs),
            ("experiment_queue", "experiment_id", sorted(self.experiments)),
            ("research_identity", "research_id", sorted(self.research)),
            ("hypothesis_versions", "hypothesis_id", hyps),
            ("integrity_audits", "hypothesis_id", hyps),
            ("failures", "hypothesis_id", hyps),
            ("hypotheses", "hypothesis_id", hyps),
            ("research_notes", "mechanism_id", sorted(self.mechanisms)),
            ("claims", "source_id", sorted(self.sources)),
            ("sources", "source_id", sorted(self.sources)),
        ]

    def purge(self) -> None:
        runs = sorted(self.runs)
        with db.tx() as cur:
            if runs:
                cur.execute(
                    "DELETE FROM experiment_trace WHERE parent_run_id = ANY(%s) "
                    "OR child_run_id = ANY(%s)",
                    (runs, runs),
                )
            for table, column, values in self._ordered():
                if not values:
                    continue
                cur.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s)", (values,))

    def counts(self) -> dict[str, int]:
        """Read every one of those tables back, so cleanup can be proven."""
        runs = sorted(self.runs)
        leftover: dict[str, int] = {}
        with db.tx() as cur:
            if runs:
                # `experiment_trace` names parent_run_id and child_run_id;
                # there is no single run_id column to match on.
                cur.execute(
                    "SELECT count(*) AS n FROM experiment_trace "
                    "WHERE parent_run_id = ANY(%s) OR child_run_id = ANY(%s)",
                    (runs, runs),
                )
                traced = cur.fetchone()["n"]
                if traced:
                    leftover["experiment_trace"] = traced
            for table, column, values in self._ordered():
                if not values:
                    continue
                cur.execute(
                    f"SELECT count(*) AS n FROM {table} WHERE {column} = ANY(%s)", (values,)
                )
                count = cur.fetchone()["n"]
                if count:
                    leftover[table] = count
        return leftover


_REGISTRY = Registry()


@pytest.fixture(scope="session", autouse=True)
def _session() -> None:
    """Apply the schema, install the canned answers, and clean up afterwards."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("schema.sql", "schema_evidence_ledger.sql", "schema_experiment.sql"):
        apply_schema((root / "db" / name).read_text(encoding="utf-8"))
    _install_canned_llm()
    try:
        yield
    finally:
        _REGISTRY.purge()


# ---------------------------------------------------------------------
# The loop fixture
# ---------------------------------------------------------------------


@dataclass
class Loop:
    """One complete pipeline run and the queued experiment it produced."""

    run: PipelineRun
    experiment_id: str
    hypothesis_id: str
    research_id: str
    version_id: int

    @property
    def source_id(self) -> str:
        return self.run.source.source_id

    @property
    def mechanism_id(self) -> str:
        return self.run.mechanisms[0].mechanism_id

    def spec(self, **overrides: Any) -> ExperimentSpec:
        """The `ExperimentSpec` a runner would hand an engine for this job."""
        with db.tx() as cur:
            cur.execute(
                "SELECT yaml_spec FROM hypothesis_versions WHERE version_id = %s",
                (self.version_id,),
            )
            row = cur.fetchone()
        fields: dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "research_id": self.research_id,
            "version_id": self.version_id,
            "rule_spec": dict(row["yaml_spec"]) if row else {},
            "dataset": "l2-reference-book",
            "dataset_version": "l2-2026-10-frozen",
            "universe": {"assets": ["BTC/USDT", "ETH/USDT"]},
            "timeframe": "5m",
            "feature_version": "features-2026-10-01",
            "execution_model": {"signal_time": "close of bar k", "price": "mid"},
            "cost_model": {"commission_bps": 4.0, "spread_bps": 1.0, "slippage_bps": 0.5},
            "parameters": {},
            "random_seed": 20261007,
        }
        fields.update(overrides)
        return ExperimentSpec(**fields)


def _register(run: PipelineRun) -> None:
    """Record everything one pipeline run created, for cleanup."""
    _REGISTRY.sources.add(run.source.source_id)
    _REGISTRY.mechanisms.update(m.mechanism_id for m in run.mechanisms)
    _REGISTRY.hypotheses.update(run.hypothesis_ids)
    for job in run.queued:
        _REGISTRY.experiments.add(job.experiment_id)
        _REGISTRY.research.add(job.research_id)


@pytest.fixture
def loop() -> Loop:
    """A fresh source through every Layer 1 stage, to the queue boundary.

    Fresh per test on purpose: `start_run` advances the queue row to `claimed`
    and `complete_run` to `completed`, so a shared loop could not be run twice.
    """
    _reset_canned_counters()
    run = run_pipeline(
        _source_in(uuid.uuid4().hex[:8]),
        protocols=_EveryHypothesis({"": _complete_protocol()}),
        max_mechanisms=1,
        hypotheses_per_mechanism=5,
        require_integrity=True,
    )
    _register(run)
    assert run.queued, f"nothing queued: {run.summary()}"
    job = run.queued[0]
    return Loop(
        run=run,
        experiment_id=job.experiment_id,
        hypothesis_id=job.hypothesis_id,
        research_id=job.research_id,
        version_id=job.version_id,
    )


def _start(loop: Loop, **overrides: Any) -> runner.RunRecord:
    """Claim the loop's experiment and pin a real dataset."""
    fields: dict[str, Any] = {
        "dataset": "l2-reference-book",
        "dataset_version": "l2-2026-10-frozen",
        "timeframe": "5m",
        "feature_version": "features-2026-10-01",
        "execution_model": {"signal_time": "close of bar k", "price": "mid"},
        "cost_model": {"commission_bps": 4.0, "spread_bps": 1.0},
        "random_seed": 20261007,
    }
    fields.update(overrides)
    record = runner.start_run(loop.experiment_id, **fields)
    _REGISTRY.runs.add(record.run_id)
    return record


def _complete(loop: Loop, record: runner.RunRecord) -> runner.RunRecord:
    """Run the stub adapter against the loop's spec and record the result."""
    return runner.complete_run(record.run_id, StubAdapter().run(loop.spec()))


# ---------------------------------------------------------------------
# 1. The milestone, end to end
# ---------------------------------------------------------------------


def test_source_reaches_a_result_row_through_every_stage(loop: Loop) -> None:
    """source -> ... -> experiment_results, with an artifact at each hop."""
    run = loop.run

    # Hop 1: the source.
    with db.tx() as cur:
        cur.execute("SELECT source_id FROM sources WHERE source_id = %s", (loop.source_id,))
        assert cur.fetchone() is not None

    # Hop 2: evidence claims, typed by the ledger and attributed to the model.
    assert run.claim_ids, "the evidence stage recorded nothing"
    with db.tx() as cur:
        cur.execute(
            "SELECT claim_type::text AS claim_type, source_id FROM claims "
            "WHERE source_id = %s",
            (loop.source_id,),
        )
        claims = cur.fetchall()
    assert len(claims) == len(run.claim_ids)
    # Model output can only ever be filed as model_inference: the signature of
    # the recording function has no claim_type parameter to get wrong.
    assert {row["claim_type"] for row in claims} == {"model_inference"}

    # Hop 3: mechanism, as an economic process rather than a signal.
    with db.tx() as cur:
        cur.execute(
            "SELECT mechanism_name, observation FROM research_notes WHERE mechanism_id = %s",
            (loop.mechanism_id,),
        )
        mechanism = cur.fetchone()
    assert mechanism is not None
    assert mechanism["mechanism_name"] == "order_flow_imbalance"

    # Hop 4: hypotheses, each with a falsifiable claim.
    assert len(run.hypothesis_ids) == 5
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM hypotheses WHERE hypothesis_id = ANY(%s)",
            (list(run.hypothesis_ids),),
        )
        assert cur.fetchone()["n"] == len(run.hypothesis_ids)

    # Hop 5: formalized RuleSpec, versioned and hash-addressed.
    spec = run.specs[loop.hypothesis_id]
    assert spec.hypothesis_id == loop.hypothesis_id
    with db.tx() as cur:
        cur.execute(
            "SELECT yaml_spec, spec_sha256, model_used FROM hypothesis_versions "
            "WHERE version_id = %s",
            (loop.version_id,),
        )
        version = cur.fetchone()
    # The stored hash addresses the RULE, not the record that carries it:
    # hashing the returned record would fold in version_id and creation
    # timestamp and match nothing.
    assert version["spec_sha256"] == RuleSpec.model_validate(version["yaml_spec"]).spec_sha256()
    # The offline provider stamps itself, so no artifact can be mistaken for a
    # real model's reasoning.
    assert version["model_used"] == "deterministic/v1"

    # Hop 6: the hostile critique, retained with its scores and its reason.
    verdict = run.critiques[loop.hypothesis_id]
    assert verdict.verdict == "PASS"
    assert verdict.reason.strip()
    assert set(verdict.scores) >= {"measurable", "no_future_leakage", "falsifiable"}

    # Hop 7: the integrity gate, which recorded why it let this one through.
    assert run.audits[loop.hypothesis_id] == "pass"
    audit = integrity.get_latest_audit(loop.hypothesis_id)
    assert audit is not None
    assert audit.missing_protocol_fields == []
    assert audit.blocking_reasons == []
    assert integrity.gate_ready(loop.hypothesis_id)

    # Hop 8: the queue boundary. Layer 1 stops here.
    assert get_settings().llm_mode == "deterministic"
    # Stage advancement is per-hypothesis and resets between them, so the run's
    # final position says nothing about this hypothesis. Its own queue record
    # does.
    queued_events = [
        entry
        for entry in run.log
        if entry["stage"] == "queue" and entry.get("experiment") == loop.experiment_id
    ]
    assert len(queued_events) == 1
    assert queued_events[0]["research_id"] == loop.research_id
    job = next(j for j in run.queued if j.experiment_id == loop.experiment_id)
    # Layer 1 cannot own a dataset, so it says so rather than guessing one.
    assert job.data_version == PENDING_DATA_VERSION

    # Hop 9: the run.
    record = _start(loop)
    assert record.status == "running"
    assert record.backend == "stub"
    assert record.dataset_version == "l2-2026-10-frozen"
    with db.tx() as cur:
        cur.execute("SELECT status::text AS status FROM experiment_queue WHERE experiment_id = %s",
                    (loop.experiment_id,))
        assert cur.fetchone()["status"] == "claimed"

    # Hop 10: the result row, read back with plain SQL.
    finished = _complete(loop, record)
    assert finished.status == "completed"
    with db.tx() as cur:
        cur.execute(
            "SELECT net_return, gross_return, trade_count, sharpe, max_drawdown "
            "FROM experiment_results WHERE run_id = %s",
            (record.run_id,),
        )
        result = cur.fetchone()
    assert result is not None
    assert result["net_return"] is not None
    with db.tx() as cur:
        cur.execute("SELECT status::text AS status FROM experiment_queue WHERE experiment_id = %s",
                    (loop.experiment_id,))
        assert cur.fetchone()["status"] == "completed"


# ---------------------------------------------------------------------
# 2. Provenance survives the whole loop
# ---------------------------------------------------------------------


def test_provenance_reaches_back_to_the_original_source(loop: Loop) -> None:
    """From the recorded run, the original source is still reachable."""
    record = _complete(loop, _start(loop))

    with db.tx() as cur:
        cur.execute(
            "SELECT hypothesis_id, research_id, dataset_version FROM experiment_runs "
            "WHERE run_id = %s",
            (record.run_id,),
        )
        run_row = cur.fetchone()
        assert run_row["hypothesis_id"] == loop.hypothesis_id

        cur.execute(
            "SELECT hypothesis_id, source_ids, rule_version, data_version "
            "FROM research_identity WHERE research_id = %s",
            (run_row["research_id"],),
        )
        identity = cur.fetchone()
    assert identity["hypothesis_id"] == loop.hypothesis_id
    # The research identity still names the Layer 1 sentinel: Layer 2 pinned a
    # dataset on the run, and did not rewrite what Layer 1 recorded.
    assert identity["data_version"] == PENDING_DATA_VERSION
    assert record.dataset_version != identity["data_version"]

    assert identity["source_ids"] == [loop.source_id]

    with db.tx() as cur:
        cur.execute(
            "SELECT source_id, content_sha256 FROM sources WHERE source_id = %s",
            (loop.source_id,),
        )
        source = cur.fetchone()
        cur.execute("SELECT content_sha256 FROM sources WHERE source_id = %s",
                    (loop.source_id,))
    assert source is not None
    assert source["source_id"] == loop.source_id

    # And the rule version the identity pins is the version that was run.
    with db.tx() as cur:
        cur.execute(
            "SELECT hypothesis_id FROM hypothesis_versions WHERE version_id = %s",
            (loop.version_id,),
        )
        assert cur.fetchone()["hypothesis_id"] == loop.hypothesis_id
    assert identity["rule_version"] == str(loop.version_id)


# ---------------------------------------------------------------------
# 3. Provenance mismatch is refused
# ---------------------------------------------------------------------


def test_start_run_refuses_a_queue_row_whose_identities_disagree(loop: Loop) -> None:
    """A queue row mixing a hypothesis with another one's spec is refused.

    The row is inserted directly because `experiment_queue` freezes its
    identity columns on UPDATE: this is precisely the corruption the guard
    exists to catch, and it cannot be produced through the sanctioned path.
    """
    other = next(h for h in loop.run.hypothesis_ids if h != loop.hypothesis_id)
    with db.tx() as cur:
        cur.execute(
            "SELECT version_id FROM hypothesis_versions WHERE hypothesis_id = %s "
            "ORDER BY version_id LIMIT 1",
            (other,),
        )
        foreign_version = cur.fetchone()["version_id"]
        cur.execute(
            "SELECT source_ids FROM research_identity WHERE research_id = %s",
            (loop.research_id,),
        )
        source_ids = cur.fetchone()["source_ids"]
        cur.execute(
            "SELECT feature_set FROM experiment_queue WHERE experiment_id = %s",
            (loop.experiment_id,),
        )
        feature_set = cur.fetchone()["feature_set"]

        # A second identity for the same hypothesis, so the forged queue row can
        # carry a research_id that is not already spoken for.
        forged_research_id = f"R_{uuid.uuid4().hex[:16]}"
        cur.execute(
            "INSERT INTO research_identity (research_id, hypothesis_id, source_ids, "
            "feature_set, rule_version, data_version, model_used, prompt_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'deterministic/v1', 'l1-v1')",
            (
                forged_research_id,
                loop.hypothesis_id,
                source_ids,
                feature_set,
                str(foreign_version),
                PENDING_DATA_VERSION,
            ),
        )
        _REGISTRY.research.add(forged_research_id)

        forged_experiment_id = f"EXP_{uuid.uuid4().hex[:10]}"
        cur.execute(
            "INSERT INTO experiment_queue (experiment_id, research_id, hypothesis_id, "
            "version_id, feature_set, rule_version, data_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                forged_experiment_id,
                forged_research_id,
                loop.hypothesis_id,
                foreign_version,
                feature_set,
                str(foreign_version),
                "l2-2026-10-frozen",
            ),
        )
        _REGISTRY.experiments.add(forged_experiment_id)

    with pytest.raises(RunStateError, match="mixes identities"):
        runner.start_run(forged_experiment_id, dataset_version="l2-2026-10-frozen")

    # Nothing was created by the refused claim.
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM experiment_runs WHERE experiment_id = %s",
            (forged_experiment_id,),
        )
        assert cur.fetchone()["n"] == 0


def test_start_run_refuses_an_experiment_layer_1_never_queued(loop: Loop) -> None:
    """Only a critic-approved hypothesis may reach an engine."""
    with pytest.raises(RunStateError, match="not in the Layer 1 queue"):
        runner.start_run("EXP_does_not_exist", dataset_version="l2-2026-10-frozen")


# ---------------------------------------------------------------------
# 4. An unpinned dataset is refused
# ---------------------------------------------------------------------


def test_start_run_refuses_the_pending_dataset_sentinel(loop: Loop) -> None:
    """A run cannot start while the dataset is still Layer 1's placeholder."""
    with pytest.raises(RunStateError, match="sentinel data_version") as caught:
        runner.start_run(loop.experiment_id)
    # The message has to name the sentinel: the operator reading it is being
    # told what to replace, not just that something is wrong.
    assert PENDING_DATA_VERSION in str(caught.value)

    # Pinning it is the whole remedy.
    record = _start(loop)
    assert record.dataset_version == "l2-2026-10-frozen"


# ---------------------------------------------------------------------
# 5. Invalid transitions
# ---------------------------------------------------------------------


def test_completing_a_run_twice_is_refused(loop: Loop) -> None:
    """The second completion is a claim about a run that already finished."""
    record = _start(loop)
    _complete(loop, record)

    with pytest.raises(RunStateError, match="is 'completed', not 'running'"):
        runner.complete_run(record.run_id, StubAdapter().run(loop.spec()))

    # And the recorded result is untouched by the refused second write.
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM experiment_results WHERE run_id = %s",
                    (record.run_id,))
        assert cur.fetchone()["n"] == 1


def test_completing_a_run_that_never_started_is_refused(loop: Loop) -> None:
    """A run that has already failed cannot be completed after the fact."""
    record = _start(loop)
    runner.fail_run(record.run_id, "data_error", "the pinned snapshot had no bars for the window")

    with pytest.raises(RunStateError, match="is 'failed', not 'running'"):
        runner.complete_run(record.run_id, StubAdapter().run(loop.spec()))

    # A broken run has no result row: an empty one would read as a flat finding.
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM experiment_results WHERE run_id = %s",
                    (record.run_id,))
        assert cur.fetchone()["n"] == 0


def test_a_completed_experiment_is_not_claimed_again(loop: Loop) -> None:
    """The queue row advanced with its run, so a finished experiment is closed.

    A second run of a *claimed* experiment is legal -- parameter sweeps and
    re-runs are the point of storing several runs per experiment. What is
    refused is claiming an experiment whose work is already recorded.
    """
    _complete(loop, _start(loop))
    with pytest.raises(RunStateError, match="is 'completed'; Layer 2 may only claim"):
        runner.start_run(loop.experiment_id, dataset_version="l2-2026-10-frozen")


def test_a_claimed_experiment_may_be_run_again(loop: Loop) -> None:
    """Several attempts per experiment is the design, not a leak."""
    first = _start(loop)
    second = runner.start_run(loop.experiment_id, dataset_version="l2-2026-11-frozen")
    _REGISTRY.runs.add(second.run_id)

    assert second.run_id != first.run_id
    assert second.dataset_version == "l2-2026-11-frozen"
    assert first.dataset_version == "l2-2026-10-frozen"
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM experiment_runs WHERE experiment_id = %s",
            (loop.experiment_id,),
        )
        assert cur.fetchone()["n"] == 2


# ---------------------------------------------------------------------
# 6. The result is typed columns, never prose
# ---------------------------------------------------------------------


def test_net_return_is_a_real_column_not_a_jsonb_field(loop: Loop) -> None:
    """A headline number that cannot be selected by name was never recorded."""
    record = _complete(loop, _start(loop))

    with db.tx() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'experiment_results' AND column_name = ANY(%s)",
            (list(metric_columns()),),
        )
        columns = {row["column_name"]: row["data_type"] for row in cur.fetchall()}
    # Every metric the adapter emits addresses a real typed column.
    assert set(metric_columns()) <= set(columns)
    assert columns["net_return"] == "double precision"

    with db.tx() as cur:
        cur.execute(
            "SELECT net_return, extra_metrics FROM experiment_results WHERE run_id = %s",
            (record.run_id,),
        )
        result = cur.fetchone()
    assert result["net_return"] is not None
    assert "net_return" not in result["extra_metrics"]
    # Cost is subtracted, so gross can never sit below net on this row.
    assert result["net_return"] < record.gross_return

    # stdout exists, and is explicitly not the record.
    with db.tx() as cur:
        cur.execute("SELECT stdout FROM experiment_artifacts WHERE run_id = %s",
                    (record.run_id,))
        assert "no data was read" in cur.fetchone()["stdout"]


def test_a_completed_run_must_report_net_return(loop: Loop) -> None:
    """Gross without net is a marketing number, so it is refused."""
    record = _start(loop)
    stub = StubAdapter()
    gross_only = stub.run(loop.spec()).model_copy(
        update={"metrics": {"gross_return": 0.42}}
    )
    with pytest.raises(RunStateError, match="reported no net_return"):
        runner.complete_run(record.run_id, gross_only)
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM experiment_results WHERE run_id = %s",
                    (record.run_id,))
        assert cur.fetchone()["n"] == 0


# ---------------------------------------------------------------------
# 7. Negative knowledge closes the loop
# ---------------------------------------------------------------------


def test_a_refutation_writes_into_layer_1_failures_and_the_critic_sees_it(
    loop: Loop,
) -> None:
    """The return path: a verdict becomes knowledge the next generation reads.

    This is the test that distinguishes a closed loop from an open one. Without
    the `failures` write the verdict is a note in a table nobody reads, and the
    pipeline will propose the same idea again with no memory of having tested
    it.
    """
    record = _complete(loop, _start(loop))

    failures_before = [
        f for f in queueing.list_failures(limit=500) if f.hypothesis_id == loop.hypothesis_id
    ]
    assert not failures_before

    verdict = analyst.Verdict(
        run_id=record.run_id,
        hypothesis_id=loop.hypothesis_id,
        kind="refute",
        reasoning=(
            "The recorded net return did not survive the declared cost model, so the "
            "effect is not measurable under this protocol."
        ),
        test_valid=True,
        mechanism_verdict="the stated mechanism did not produce a net return",
    )
    verdict_id = analyst.record_verdict(verdict)
    assert verdict_id.startswith("VER_")

    # The verdict itself is readable back.
    stored = analyst.latest_verdict(loop.hypothesis_id)
    assert stored is not None
    assert stored.kind == "refute"

    # ... and the negative knowledge landed in Layer 1's own table, at the
    # Layer 2 stage. This is the write that closes the loop.
    failures = [
        f for f in queueing.list_failures(limit=500) if f.hypothesis_id == loop.hypothesis_id
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.stage == "layer2_backtest"
    assert failure.reported_by == "layer2.result_analyst"
    assert failure.failure_type in analyst.REFUTE_FAILURE_TYPES
    assert record.run_id in failure.reason

    with db.tx() as cur:
        cur.execute(
            "SELECT stage, failure_type FROM failures WHERE hypothesis_id = %s "
            "AND stage = 'layer2_backtest'",
            (loop.hypothesis_id,),
        )
        assert cur.fetchone() is not None

    # The Research Critic reads `failures` before judging anything new, so the
    # next critique of this mechanism carries the refutation as context.
    digest = critic._prior_failure_digest(loop.mechanism_id)
    assert digest["total_failures"] >= 1
    assert any(
        row["hypothesis_id"] == loop.hypothesis_id and row["stage"] == "layer2_backtest"
        for row in digest["same_mechanism"]
    )
    assert digest["from_critic"] >= 0


def test_a_support_verdict_records_no_failure(loop: Loop) -> None:
    """A failure database that records successes is a log, not knowledge."""
    record = _complete(loop, _start(loop))
    analyst.record_verdict(
        analyst.Verdict(
            run_id=record.run_id,
            hypothesis_id=loop.hypothesis_id,
            kind="support",
            reasoning="stub plumbing test only; not a finding",
        )
    )
    assert not [
        f
        for f in queueing.list_failures(limit=500)
        if f.hypothesis_id == loop.hypothesis_id and f.stage == "layer2_backtest"
    ]


def test_the_analyst_refuses_a_stub_run_as_evidence(loop: Loop) -> None:
    """Whatever the fabricated numbers say, the verdict names the engine.

    The stub reports `inconclusive`, and no fabricated draw reaches the
    support branch, so the loop never files stub output as knowledge. The
    header's disclaimer is what makes that legible to a reader.
    """
    record = _complete(loop, _start(loop))
    verdict = analyst.analyse(record.run_id)
    assert verdict.kind != "support"
    assert "NOT A REAL ENGINE" in verdict.reasoning

    with db.tx() as cur:
        cur.execute(
            "SELECT extra_metrics FROM experiment_results WHERE run_id = %s",
            (record.run_id,),
        )
        extra = cur.fetchone()["extra_metrics"]
    # The row says so itself, in its own JSONB, even if the flag is not read.
    assert extra["fabricated"] is True
    assert "never evidence" in extra["warning"]


# ---------------------------------------------------------------------
# 8. The trace DAG
# ---------------------------------------------------------------------


def test_a_derived_run_is_recorded_in_the_trace(loop: Loop, tmp_path: Any) -> None:
    """A second attempt is an edge with a reason, not an unexplained rerun."""
    del tmp_path
    parent = _complete(loop, _start(loop))

    # A second experiment from a second source, so the child run is a distinct
    # experiment rather than a second attempt on the same queue row.
    _reset_canned_counters()
    sibling = run_pipeline(
        _source_in(uuid.uuid4().hex[:8]),
        protocols=_EveryHypothesis({"": _complete_protocol()}),
        max_mechanisms=1,
        hypotheses_per_mechanism=5,
        require_integrity=True,
    )
    _register(sibling)
    assert sibling.queued
    child_loop = Loop(
        run=sibling,
        experiment_id=sibling.queued[0].experiment_id,
        hypothesis_id=sibling.queued[0].hypothesis_id,
        research_id=sibling.queued[0].research_id,
        version_id=sibling.queued[0].version_id,
    )
    child = _complete(child_loop, _start(child_loop))

    edge_id = analyst.link_run(
        parent.run_id,
        child.run_id,
        reason="replication",
        rationale=(
            "the first attempt was recorded under one dataset pin; this repeats it "
            "under a second pin to check the result is not an artefact of the first"
        ),
        child_hypothesis_id=child_loop.hypothesis_id,
    )
    assert edge_id.startswith("EDGE_")

    with db.tx() as cur:
        cur.execute(
            "SELECT parent_run_id, child_run_id, reason::text AS reason, rationale, "
            "child_hypothesis_id FROM experiment_trace WHERE edge_id = %s",
            (edge_id,),
        )
        edge = cur.fetchone()
    assert edge["parent_run_id"] == parent.run_id
    assert edge["child_run_id"] == child.run_id
    assert edge["reason"] == "replication"
    assert edge["rationale"].strip()
    assert edge["child_hypothesis_id"] == child_loop.hypothesis_id


def test_the_trace_refuses_a_self_loop(loop: Loop) -> None:
    """A run cannot be its own ancestor."""
    record = _complete(loop, _start(loop))
    with pytest.raises(analyst.TraceLinkError, match="self"):
        analyst.link_run(
            record.run_id, record.run_id, reason="revision", rationale="because"
        )
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM experiment_trace "
            "WHERE parent_run_id = %s OR child_run_id = %s",
            (record.run_id, record.run_id),
        )
        assert cur.fetchone()["n"] == 0


def test_the_trace_requires_a_rationale(loop: Loop) -> None:
    """An unjustified edge is a rerun, not progress."""
    record = _complete(loop, _start(loop))
    _reset_canned_counters()
    sibling = run_pipeline(
        _source_in(uuid.uuid4().hex[:8]),
        protocols=_EveryHypothesis({"": _complete_protocol()}),
        max_mechanisms=1,
        hypotheses_per_mechanism=5,
        require_integrity=True,
    )
    _register(sibling)
    child_loop = Loop(
        run=sibling,
        experiment_id=sibling.queued[0].experiment_id,
        hypothesis_id=sibling.queued[0].hypothesis_id,
        research_id=sibling.queued[0].research_id,
        version_id=sibling.queued[0].version_id,
    )
    child = _complete(child_loop, _start(child_loop))

    with pytest.raises(analyst.TraceLinkError, match="rationale"):
        analyst.link_run(
            record.run_id, child.run_id, reason="revision", rationale="   "
        )

    # The database says the same thing, for rows that never went through Python.
    with pytest.raises(psycopg.errors.NotNullViolation):
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO experiment_trace (edge_id, parent_run_id, child_run_id, "
                "reason, rationale) VALUES (%s, %s, %s, 'revision', NULL)",
                (f"EDGE_{uuid.uuid4().hex[:12]}", record.run_id, child.run_id),
            )


def test_the_database_refuses_a_self_loop_edge(loop: Loop) -> None:
    """The CHECK is the last line: SQL that bypasses Python is still refused."""
    record = _complete(loop, _start(loop))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO experiment_trace (edge_id, parent_run_id, child_run_id, "
                "reason, rationale) VALUES (%s, %s, %s, 'revision', 'same run twice')",
                (f"EDGE_{uuid.uuid4().hex[:12]}", record.run_id, record.run_id),
            )


# ---------------------------------------------------------------------
# 9. A stub result is reportable and is never evidence
# ---------------------------------------------------------------------


def test_a_stub_run_is_reportable_and_flagged_as_fake(loop: Loop) -> None:
    """The single-row read surface says plainly that no real engine ran."""
    record = _complete(loop, _start(loop))

    report = runner.run_report(record.run_id)
    assert report is not None
    assert report["backend"] == "stub"
    assert report["is_real_engine"] is False
    assert report["hypothesis_statement"]
    assert report["expected_mechanism"]
    assert report["net_return"] is not None
    # stdout is deliberately absent from the read surface.
    assert "stdout" not in report

    with db.tx() as cur:
        cur.execute(
            "SELECT is_available, is_real_engine FROM execution_backends "
            "WHERE backend = 'stub'"
        )
        backend = cur.fetchone()
    assert backend["is_available"] is True
    assert backend["is_real_engine"] is False


def test_the_stub_adapter_is_deterministic_and_spec_dependent() -> None:
    """One spec, one set of numbers; two different specs, two different sets."""
    spec = ExperimentSpec(
        experiment_id="EXP_000001",
        hypothesis_id="H0001",
        research_id="R_deadbeefdeadbeef",
        version_id=1,
        rule_spec={"name": "x"},
        random_seed=99,
    )

    first = StubAdapter().run(spec)
    assert first.metrics == StubAdapter().run(spec).metrics

    # An explicit seed is the reproducibility promise, so it wins over the
    # spec hash: the same seed is the same experiment.
    assert StubAdapter().run(spec.model_copy(update={"hypothesis_id": "H0002"})).metrics == (
        first.metrics
    )

    # With no seed, the spec's own content decides -- so two genuinely
    # different experiments do not land on the same numbers by accident.
    unseeded = spec.model_copy(update={"random_seed": None})
    assert StubAdapter().run(unseeded).metrics == StubAdapter().run(unseeded).metrics
    assert StubAdapter().run(unseeded).metrics != StubAdapter().run(
        unseeded.model_copy(update={"hypothesis_id": "H0002"})
    ).metrics

    # Metrics span the whole typed result surface, so the write path is
    # exercised end to end rather than on one convenient column.
    assert set(first.metrics) == set(metric_columns())
    assert first.status == "completed"
    # The stub never claims to have supported anything.
    assert first.outcome != "supported"
    assert first.extra_metrics["fabricated"] is True


def test_the_stub_can_be_forced_to_fail() -> None:
    """The failure path is reachable through the adapter, not a hand-built dict."""
    spec = ExperimentSpec(
        experiment_id="EXP_000001",
        hypothesis_id="H0001",
        research_id="R_deadbeefdeadbeef",
        version_id=1,
        rule_spec={"name": "x"},
    )
    forced = StubAdapter(fail=True).run(spec)
    assert forced.status == "failed"
    assert forced.outcome == "errored"
    assert forced.metrics == {}
    assert forced.failure_reason

    assert StubAdapter().run(spec, outcome_override="errored").status == "failed"


# ---------------------------------------------------------------------
# 10. The integrity gate is wired in
# ---------------------------------------------------------------------


def test_no_protocol_means_not_queued() -> None:
    """With no protocol the audit is incomplete and nothing reaches Layer 2.

    The gate being present is not the claim; the gate being *called* by the
    pipeline is. So this asserts an audit row exists for every hypothesis that
    got that far, says what is missing, and that the queue stayed empty.
    """
    _reset_canned_counters()
    run = run_pipeline(
        _source_in(uuid.uuid4().hex[:8]),
        protocols=None,
        max_mechanisms=1,
        hypotheses_per_mechanism=5,
        require_integrity=True,
    )
    _register(run)

    assert run.claim_ids and run.mechanisms and run.hypothesis_ids
    assert run.queued == []
    # Every hypothesis the critic passed reached the gate, and every one of
    # them was stopped there.
    assert run.audits, "the integrity stage never ran"
    for hypothesis_id, status in run.audits.items():
        assert status != "pass"
        assert not integrity.gate_ready(hypothesis_id)
        audit = integrity.get_latest_audit(hypothesis_id)
        assert audit is not None
        # The absent protocol is what blocks every one of them.
        assert audit.missing_protocol_fields
        assert any(
            reason.startswith(integrity.CHECK_PROTOCOL_COMPLETENESS)
            for reason in audit.blocking_reasons
        )
        assert audit.checklist_results[integrity.CHECK_PROTOCOL_COMPLETENESS].status != "pass"

    # The status differs across hypotheses -- `incomplete` for the first, `fail`
    # for the rest -- because the critic rejected two of the five and the
    # integrity gate counts a collision with a recorded failure against the
    # whole mechanism, not just the hypothesis it was recorded against. Both
    # are refusals; only the reason differs.
    statuses = set(run.audits.values())
    assert statuses <= {"incomplete", "fail"}

    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM experiment_queue WHERE hypothesis_id = ANY(%s)",
            (list(run.hypothesis_ids),),
        )
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM integrity_audits WHERE hypothesis_id = ANY(%s)",
            (list(run.audits),),
        )
        assert cur.fetchone()["n"] == len(run.audits)


def test_a_complete_protocol_reaches_the_queue() -> None:
    """Same source shape, same critic, same integrity module -- one difference.

    The only thing that changed from the test above is that the protocol was
    supplied, so the queue row can only be there because the gate let it
    through.
    """
    _reset_canned_counters()
    run = run_pipeline(
        _source_in(uuid.uuid4().hex[:8]),
        protocols=_EveryHypothesis({"": _complete_protocol()}),
        max_mechanisms=1,
        hypotheses_per_mechanism=5,
        require_integrity=True,
    )
    _register(run)

    assert run.queued, f"a complete protocol still queued nothing: {run.summary()}"
    queued_ids = [job.hypothesis_id for job in run.queued]
    for hypothesis_id in queued_ids:
        assert run.audits[hypothesis_id] == "pass"
        assert integrity.gate_ready(hypothesis_id)

    with db.tx() as cur:
        cur.execute(
            "SELECT experiment_id, data_version FROM experiment_queue "
            "WHERE hypothesis_id = ANY(%s)",
            (queued_ids,),
        )
        rows = cur.fetchall()
    assert {row["experiment_id"] for row in rows} == {j.experiment_id for j in run.queued}
    assert {row["data_version"] for row in rows} == {PENDING_DATA_VERSION}


def test_an_invalid_verdict_must_explain_itself(loop: Loop) -> None:
    """`test_valid=False` with no reason is refused, in the model and in the DB."""
    record = _complete(loop, _start(loop))

    with pytest.raises(ValueError, match="invalidity_reason"):
        analyst.Verdict(
            run_id=record.run_id,
            hypothesis_id=loop.hypothesis_id,
            kind="inconclusive",
            reasoning="something",
            test_valid=False,
        )

    # The CHECK catches a row written by SQL that never went through the model.
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO analyst_verdicts (verdict_id, run_id, hypothesis_id, kind, "
                "reasoning, test_valid, invalidity_reason) "
                "VALUES (%s, %s, %s, 'inconclusive', 'no reason given', FALSE, NULL)",
                (
                    f"VER_{uuid.uuid4().hex[:12]}",
                    record.run_id,
                    loop.hypothesis_id,
                ),
            )


# ---------------------------------------------------------------------
# Cleanup, proven by readback
# ---------------------------------------------------------------------


def test_no_rows_survive_this_session() -> None:
    """Purge everything this module created, then read every table back."""
    assert _REGISTRY.runs, "nothing was registered, so this proves nothing"
    assert _REGISTRY.hypotheses and _REGISTRY.sources

    leftovers = _REGISTRY.counts()
    assert leftovers != {}, "the readback found no rows of ours to check"

    _REGISTRY.purge()
    assert _REGISTRY.counts() == {}, f"rows survived cleanup: {_REGISTRY.counts()}"