"""Integration tests for the Layer 1 database schema.

These assert the guarantees the architecture claims but cannot enforce in
Python:

* evidence and formalized specs are append-only,
* the Layer 1 / Layer 2 queue is a genuine hand-off surface -- Layer 2 can
  claim a job, but cannot rewrite which research it is running,
* there is no profitability target anywhere in the persisted shape,
* the hand-off view gives Layer 2 everything it needs in one row.

Every test inserts a uniquely named chain of rows and deletes it again.
IDs carry a per-run suffix, so these tests are order independent and can
coexist with any other writer against the same database.  ``sources`` and
``hypothesis_versions`` are append-only under UPDATE triggers, so cleanup
is DELETE-only by construction; the fixtures make no attempt to reset a
row's contents.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
import yaml

from layer1 import db
from layer1.contracts import RuleSpec

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"

#: Columns Layer 2 must be able to read off a single hand-off row.
LAYER2_VIEW_COLUMNS = (
    "experiment_id",
    "research_id",
    "rule_spec",
    "source_ids",
    "feature_set",
    "rule_version",
    "data_version",
    "model_used",
    "prompt_version",
)


def _rid(kind: str) -> str:
    """A collision-proof id for one test's row."""
    return f"{kind}_{uuid.uuid4().hex[:12]}"


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

    def ids(self) -> tuple[tuple[str, str], ...]:
        return (
            ("sources", "source_id"),
            ("research_notes", "mechanism_id"),
            ("hypotheses", "hypothesis_id"),
            ("hypothesis_versions", "hypothesis_id"),
            ("research_identity", "research_id"),
            ("experiment_queue", "experiment_id"),
        )

    def delete_all(self) -> None:
        """Remove the chain, children before parents.

        Deletion order is not cosmetic: every one of these tables carries a
        foreign key, so a parent removed first raises and the surrounding
        transaction rolls back, leaving the database dirty.
        """
        with db.tx() as cur:
            cur.execute(
                "DELETE FROM failures WHERE hypothesis_id = %s",
                (self.hypothesis_id,),
            )
            cur.execute(
                "DELETE FROM experiment_queue WHERE experiment_id = %s",
                (self.experiment_id,),
            )
            cur.execute(
                "DELETE FROM research_identity WHERE research_id = %s",
                (self.research_id,),
            )
            cur.execute(
                "DELETE FROM hypothesis_versions WHERE hypothesis_id = %s",
                (self.hypothesis_id,),
            )
            cur.execute(
                "DELETE FROM hypotheses WHERE hypothesis_id = %s",
                (self.hypothesis_id,),
            )
            cur.execute(
                "DELETE FROM research_notes WHERE mechanism_id = %s",
                (self.mechanism_id,),
            )
            cur.execute(
                "DELETE FROM sources WHERE source_id = %s", (self.source_id,)
            )


@pytest.fixture
def chain() -> Chain:
    """Insert one linked source -> note -> hypothesis -> version ->
    identity -> queue chain and tear it down afterwards.

    The teardown runs in the fixture's finally block, so a failing test
    still leaves the database clean.
    """
    chain = Chain(
        source_id=_rid("TST_SRC"),
        mechanism_id=_rid("TST_MCH"),
        hypothesis_id=_rid("TST_HYP"),
        version_id=0,
        research_id="R_" + uuid.uuid4().hex[:16],
        experiment_id=_rid("TST_EXP"),
    )
    rule_spec = {
        "hypothesis_id": chain.hypothesis_id,
        "name": "OFI_Microprice_Agreement",
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
    try:
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO sources
                    (source_id, source_type, title, content, content_sha256, date, tier)
                VALUES (%s, 'note', %s, %s, %s, now(), '3')
                """,
                (
                    chain.source_id,
                    "Schema test evidence",
                    "Signed order flow imbalance precedes short-horizon drift.",
                    _sha(),
                ),
            )
            cur.execute(
                """
                INSERT INTO research_notes
                    (mechanism_id, source_id, mechanism_name, observation,
                     mechanism_description, candidate_variables)
                VALUES (%s, %s, 'OFI absorption', 'imbalance persists briefly',
                        'Informed makers consume imbalance before it reverses',
                        '{signed_volume_imbalance_1m,microprice_deviation_bps}')
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
                (
                    chain.hypothesis_id,
                    json.dumps(rule_spec),
                    _sha(),
                ),
            )
            chain = Chain(
                source_id=chain.source_id,
                mechanism_id=chain.mechanism_id,
                hypothesis_id=chain.hypothesis_id,
                version_id=cur.fetchone()["version_id"],
                research_id=chain.research_id,
                experiment_id=chain.experiment_id,
            )
            cur.execute(
                """
                INSERT INTO research_identity
                    (research_id, hypothesis_id, source_ids, feature_set,
                     rule_version, data_version, model_used, prompt_version)
                VALUES (%s, %s, %s,
                        '{signed_volume_imbalance_1m,microprice_deviation_bps}',
                        'OFI_Microprice_Agreement', 'data-v1',
                        'deterministic/v1', 'l1-v1')
                """,
                (chain.research_id, chain.hypothesis_id, [chain.source_id]),
            )
            cur.execute(
                """
                INSERT INTO experiment_queue
                    (experiment_id, research_id, hypothesis_id, version_id,
                     feature_set, rule_version, data_version)
                VALUES (%s, %s, %s, %s,
                        '{signed_volume_imbalance_1m,microprice_deviation_bps}',
                        'OFI_Microprice_Agreement', 'data-v1')
                RETURNING status
                """,
                (
                    chain.experiment_id,
                    chain.research_id,
                    chain.hypothesis_id,
                    chain.version_id,
                ),
            )
            assert cur.fetchone()["status"] == "queued"
        yield chain
    finally:
        chain.delete_all()


# ---------------------------------------------------------------------
# 1. The schema applies idempotently
# ---------------------------------------------------------------------


def test_schema_applies_twice_without_error() -> None:
    """`apply_schema` runs on every service boot, so re-running must be safe
    against an already populated database.  A boot-time crash here would take
    the whole research engine offline."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    db.apply_schema(sql)
    db.apply_schema(sql)


# ---------------------------------------------------------------------
# 2. sources is append-only
# ---------------------------------------------------------------------


def test_sources_update_is_blocked(chain: Chain) -> None:
    """A corrected source is a NEW source.  Silently editing evidence would
    invalidate every hypothesis derived from the old content."""
    with pytest.raises(psycopg.errors.RestrictViolation, match="append-only"):
        with db.tx() as cur:
            cur.execute(
                "UPDATE sources SET title = %s WHERE source_id = %s",
                ("Quietly rewritten", chain.source_id),
            )


def test_sources_content_cannot_be_swapped(chain: Chain) -> None:
    """The content hash is tamper evidence; content itself must be frozen too."""
    with pytest.raises(psycopg.errors.RestrictViolation):
        with db.tx() as cur:
            cur.execute(
                "UPDATE sources SET content = %s WHERE source_id = %s",
                ("Different evidence entirely.", chain.source_id),
            )


def test_sources_update_is_blocked_even_when_values_are_unchanged(
    chain: Chain,
) -> None:
    """The trigger is unconditional, so there is no 'harmless' UPDATE escape
    hatch that a careless retry could mistake for a successful edit."""
    with pytest.raises(psycopg.errors.RestrictViolation):
        with db.tx() as cur:
            cur.execute(
                "UPDATE sources SET title = title WHERE source_id = %s",
                (chain.source_id,),
            )


def test_sources_row_is_actually_persisted(chain: Chain) -> None:
    """Sanity on the fixture itself: the trigger test is only meaningful if a
    row with this id really exists."""
    with db.tx() as cur:
        cur.execute(
            "SELECT content_sha256, tier FROM sources WHERE source_id = %s",
            (chain.source_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["tier"] == "3"
    assert row["content_sha256"]


# ---------------------------------------------------------------------
# 3. hypothesis_versions is append-only
# ---------------------------------------------------------------------


def test_hypothesis_versions_update_is_blocked(chain: Chain) -> None:
    """A formalized spec is frozen.  Re-formalizing the same content is a
    no-op; changed content must become a new version, not an edit."""
    with pytest.raises(psycopg.errors.RestrictViolation, match="append-only"):
        with db.tx() as cur:
            cur.execute(
                "UPDATE hypothesis_versions SET yaml_spec = %s::jsonb "
                "WHERE version_id = %s",
                (json.dumps({"tampered": True}), chain.version_id),
            )


def test_hypothesis_versions_prompt_version_cannot_be_rewritten(
    chain: Chain,
) -> None:
    """Provenance is the whole point of the row; a mutable prompt_version
    would let an experiment be re-attributed to a prompt that never ran."""
    with pytest.raises(psycopg.errors.RestrictViolation):
        with db.tx() as cur:
            cur.execute(
                "UPDATE hypothesis_versions SET prompt_version = 'l1-v99' "
                "WHERE version_id = %s",
                (chain.version_id,),
            )


def test_identical_spec_cannot_be_inserted_twice(chain: Chain) -> None:
    """(hypothesis_id, spec_sha256) is unique: content addressing means the
    same rule is stored once, however many times it is formalized."""
    with db.tx() as cur:
        cur.execute(
            "SELECT yaml_spec, spec_sha256, model_used, prompt_version "
            "FROM hypothesis_versions WHERE version_id = %s",
            (chain.version_id,),
        )
        row = cur.fetchone()
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO hypothesis_versions
                    (hypothesis_id, yaml_spec, spec_sha256, model_used, prompt_version)
                VALUES (%s, %s::jsonb, %s, %s, %s)
                """,
                (
                    chain.hypothesis_id,
                    json.dumps(row["yaml_spec"]),
                    row["spec_sha256"],
                    row["model_used"],
                    row["prompt_version"],
                ),
            )


# ---------------------------------------------------------------------
# 4. The experiment queue is a real hand-off surface
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE experiment_queue SET experiment_id = 'TST_EXP_rewritten' "
            "WHERE experiment_id = %s",
            id="experiment_id",
        ),
        pytest.param(
            "UPDATE experiment_queue SET hypothesis_id = 'TST_HYP_other' "
            "WHERE experiment_id = %s",
            id="hypothesis_id",
        ),
        pytest.param(
            "UPDATE experiment_queue SET rule_version = 'SomeOtherRule' "
            "WHERE experiment_id = %s",
            id="rule_version",
        ),
        pytest.param(
            "UPDATE experiment_queue SET feature_set = '{leaked_feature}' "
            "WHERE experiment_id = %s",
            id="feature_set",
        ),
        pytest.param(
            "UPDATE experiment_queue SET version_id = version_id + 1000 "
            "WHERE experiment_id = %s",
            id="version_id",
        ),
    ],
)
def test_experiment_queue_identity_fields_are_frozen(
    chain: Chain, statement: str
) -> None:
    """Layer 1 wrote these; Layer 2 must not be able to swap which research
    an experiment id refers to after the fact."""
    with pytest.raises(psycopg.errors.RestrictViolation):
        with db.tx() as cur:
            cur.execute(statement, (chain.experiment_id,))


def test_layer2_can_claim_a_queued_experiment(chain: Chain) -> None:
    """The guard on the opposite side: an over-strict trigger would lock
    Layer 2 out of the queue and the pipeline would deadlock silently."""
    with db.tx() as cur:
        cur.execute(
            "UPDATE experiment_queue SET status = 'claimed', claimed_at = now() "
            "WHERE experiment_id = %s RETURNING status, claimed_at",
            (chain.experiment_id,),
        )
        row = cur.fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_at"] is not None


def test_claimed_state_persists_across_connections(chain: Chain) -> None:
    """The claim must be durable, not a rolled-back no-op: Layer 2 polls on a
    different connection from the one it wrote on."""
    with db.tx() as cur:
        cur.execute(
            "UPDATE experiment_queue SET status = 'claimed', claimed_at = now() "
            "WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
    with db.tx() as cur:
        cur.execute(
            "SELECT status, claimed_at FROM experiment_queue WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
        row = cur.fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_at"] is not None


def test_layer2_can_complete_a_claimed_experiment(chain: Chain) -> None:
    with db.tx() as cur:
        cur.execute(
            "UPDATE experiment_queue SET status = 'claimed', claimed_at = now() "
            "WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
    with db.tx() as cur:
        cur.execute(
            "UPDATE experiment_queue SET status = 'completed', completed_at = now() "
            "WHERE experiment_id = %s RETURNING status, completed_at",
            (chain.experiment_id,),
        )
        row = cur.fetchone()
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


def test_status_is_a_closed_enum_not_free_text(chain: Chain) -> None:
    """An unparseable lifecycle state would leave Layer 2 to defend against it."""
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        with db.tx() as cur:
            cur.execute(
                "UPDATE experiment_queue SET status = 'done' WHERE experiment_id = %s",
                (chain.experiment_id,),
            )


# ---------------------------------------------------------------------
# 5. failures.stage is constrained
# ---------------------------------------------------------------------


def test_failures_rejects_an_unknown_stage(chain: Chain) -> None:
    """Negative knowledge is first class but must still be attributable to a
    real pipeline stage."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO failures (hypothesis_id, reason, failure_type, stage)
                VALUES (%s, 'Not falsifiable', 'not_falsifiable', 'vibes')
                """,
                (chain.hypothesis_id,),
            )


@pytest.mark.parametrize("stage", ["critic", "layer2_backtest"])
def test_failures_accepts_each_real_stage(chain: Chain, stage: str) -> None:
    """Both the Layer 1 critic and the Layer 2 backtester write here."""
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO failures (hypothesis_id, reason, failure_type, stage)
            VALUES (%s, 'Recorded negative result', 'not_falsifiable', %s)
            RETURNING stage, reported_by
            """,
            (chain.hypothesis_id, stage),
        )
        row = cur.fetchone()
    assert row["stage"] == stage
    assert row["reported_by"] == "layer1.research_critic"


def test_failures_rejects_an_unknown_failure_type(chain: Chain) -> None:
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO failures (hypothesis_id, reason, failure_type, stage)
                VALUES (%s, 'x', 'looked_bad', 'critic')
                """,
                (chain.hypothesis_id,),
            )


def test_a_failed_check_leaves_no_partial_row(chain: Chain) -> None:
    """The rejected INSERT must not leave a row behind for the critic to
    later read as a real rejection."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO failures (hypothesis_id, reason, failure_type, stage)
                VALUES (%s, 'x', 'not_measurable', 'nonsense')
                """,
                (chain.hypothesis_id,),
            )
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM failures WHERE hypothesis_id = %s",
            (chain.hypothesis_id,),
        )
        assert cur.fetchone()["n"] == 0


# ---------------------------------------------------------------------
# 6. The Layer 2 hand-off view
# ---------------------------------------------------------------------


def test_layer2_view_is_selectable(chain: Chain) -> None:
    """Layer 2 reads exactly this view and nothing else."""
    with db.tx() as cur:
        cur.execute(
            "SELECT * FROM experiment_queue_layer2 WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
        row = cur.fetchone()
    assert row is not None


def test_layer2_view_exposes_every_required_column(chain: Chain) -> None:
    """Named explicitly because a missing column is a Layer 2 crash at
    hand-off time, long after Layer 1 looked healthy."""
    with db.tx() as cur:
        cur.execute(
            "SELECT "
            + ", ".join(LAYER2_VIEW_COLUMNS)
            + " FROM experiment_queue_layer2 WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
        row = cur.fetchone()
    assert row is not None
    for column in LAYER2_VIEW_COLUMNS:
        assert column in row, f"hand-off view is missing {column}"


def test_layer2_view_carries_provenance_matching_the_chain(chain: Chain) -> None:
    """The view must join the real values through, not just expose the columns."""
    with db.tx() as cur:
        cur.execute(
            "SELECT * FROM experiment_queue_layer2 WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
        row = cur.fetchone()
    assert row["research_id"] == chain.research_id
    assert row["hypothesis_id"] == chain.hypothesis_id
    assert row["source_ids"] == [chain.source_id]
    assert row["rule_version"] == "OFI_Microprice_Agreement"
    assert row["data_version"] == "data-v1"
    assert row["model_used"] == "deterministic/v1"
    assert row["prompt_version"] == "l1-v1"
    assert row["feature_set"] == [
        "signed_volume_imbalance_1m",
        "microprice_deviation_bps",
    ]


def test_layer2_view_rule_spec_revalidates_under_the_l1_contract(
    chain: Chain,
) -> None:
    """What Layer 2 reads must satisfy the contract Layer 1 wrote against.

    This is the real boundary check: the persisted document is re-validated
    by the same model that produced it, so a spec that round-tripped through
    JSONB and the view cannot have silently lost or gained a field -- and
    still cannot carry a profitability target.
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT rule_spec FROM experiment_queue_layer2 WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
        rule_spec = cur.fetchone()["rule_spec"]

    parsed = RuleSpec.model_validate(rule_spec)
    assert parsed.hypothesis.null
    assert parsed.hypothesis_id == chain.hypothesis_id
    assert "expected_return" not in rule_spec
    assert yaml.safe_load(yaml.safe_dump(rule_spec)) == rule_spec


def test_layer2_view_reflects_a_claim(chain: Chain) -> None:
    """The view is not a frozen snapshot: Layer 2's status write must be
    visible to the next poll."""
    with db.tx() as cur:
        cur.execute(
            "UPDATE experiment_queue SET status = 'claimed', claimed_at = now() "
            "WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
    with db.tx() as cur:
        cur.execute(
            "SELECT status FROM experiment_queue_layer2 WHERE experiment_id = %s",
            (chain.experiment_id,),
        )
        assert cur.fetchone()["status"] == "claimed"
