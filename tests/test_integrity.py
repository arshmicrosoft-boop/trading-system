"""Tests for the Research Integrity Checker.

The gate's value is entirely in what it REFUSES, so the tests are written
around the refusals rather than around the happy path:

* a complete protocol passes,
* an empty one is incomplete and can never be pass,
* a protocol that reads information from the bar it trades on is a hard fail,
* a hypothesis that passed and is later re-audited down LEAVES the gate,
* the static lint catches prose, an empty null, and a profitability key,
* every audit is durable, with its per-item breakdown intact.

The downgrade test is the one that must never be weakened. An implementation
that took the latest *passing* audit instead of the latest audit would pass
every other test here and would fail exactly when it matters: a hypothesis
found to be leaky on re-audit would still be enqueueable. The test asserts on
``gate_ready`` after a genuine re-audit, so a fail-open regression fails it.

Each test inserts a uniquely-prefixed source -> note -> hypothesis chain and
deletes it again (children first). IDs carry a per-run suffix, so these are
order independent and safe against any other writer on the same database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from layer1 import db, integrity
from layer1.integrity import (
    ProtocolSpec,
    IntegrityInputError,
    audit,
    gate_ready,
    get_latest_audit,
    lint_spec,
    list_audits,
)


def _rid(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Chain:
    """Identifiers for one source -> note -> hypothesis chain."""

    source_id: str
    mechanism_id: str
    hypothesis_id: str

    def delete_all(self) -> None:
        with db.tx() as cur:
            cur.execute(
                "DELETE FROM integrity_audits WHERE hypothesis_id = %s",
                (self.hypothesis_id,),
            )
            cur.execute(
                "DELETE FROM failures WHERE hypothesis_id = %s",
                (self.hypothesis_id,),
            )
            cur.execute(
                "DELETE FROM hypotheses WHERE hypothesis_id = %s", (self.hypothesis_id,)
            )
            cur.execute(
                "DELETE FROM research_notes WHERE mechanism_id = %s", (self.mechanism_id,)
            )
            cur.execute("DELETE FROM sources WHERE source_id = %s", (self.source_id,))


def _insert_chain(statement: str, mechanism_text: str) -> Chain:
    """Insert one linked source -> note -> hypothesis chain.

    A factory rather than a single fixture because the cost rules key off the
    hypothesis text: whether funding is mandatory depends on what the
    hypothesis is about, so a test of that rule needs a hypothesis about that
    venue.
    """
    chain = Chain(
        source_id=_rid("TST_SRC"),
        mechanism_id=_rid("TST_MCH"),
        hypothesis_id=_rid("TST_HYP"),
    )
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO sources
                (source_id, source_type, title, content, content_sha256, date, tier)
            VALUES (%s, 'note', 'Integrity test evidence',
                    'Cross-sectional dispersion predicts reversal.', %s, now(), '3')
            """,
            (chain.source_id, uuid.uuid4().hex),
        )
        cur.execute(
            """
            INSERT INTO research_notes
                (mechanism_id, source_id, mechanism_name, observation,
                 mechanism_description, candidate_variables)
            VALUES (%s, %s, 'Liquidity provision', 'dispersion reverses', %s,
                    '{cross_sectional_dispersion,order_imbalance}')
            """,
            (chain.mechanism_id, chain.source_id, mechanism_text),
        )
        cur.execute(
            """
            INSERT INTO hypotheses
                (hypothesis_id, mechanism_id, statement, expected_mechanism,
                 candidate_variables)
            VALUES (%s, %s, %s, %s, '{cross_sectional_dispersion}')
            """,
            (chain.hypothesis_id, chain.mechanism_id, statement, mechanism_text),
        )
    return chain


@pytest.fixture
def chain() -> Chain:
    """A plain equity-style hypothesis, funding not relevant to it."""
    chain = _insert_chain(
        "High cross-sectional dispersion predicts subsequent reversal",
        "Liquidity demanders absorb price pressure and price relaxes",
    )
    try:
        yield chain
    finally:
        chain.delete_all()


@pytest.fixture
def perpetual_chain() -> Chain:
    """A hypothesis about a perpetual venue, where funding is a real cost."""
    chain = _insert_chain(
        "Elevated perpetual funding precedes forced unwinding and reversal",
        "Carried longs pay funding until forced unwinding reverses the move",
    )
    try:
        yield chain
    finally:
        chain.delete_all()


def _complete_protocol() -> dict[str, Any]:
    """A protocol with every block concrete, per reproducibility_audit.md."""
    return {
        "temporal_protocol": {
            "train": {"start": "2020-01-01", "end": "2021-12-31"},
            "validation": {"start": "2022-01-01", "end": "2022-06-30"},
            "test": {"start": "2022-07-01", "end": "2023-12-31"},
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
            "spread_model": "quoted",
            "slippage_model": "fixed_bps",
            "slippage_bps": 1.0,
            "funding": "applied",
        },
        "data": {
            "provider": "reference-vendor",
            "dataset_version": "2024-03-snapshot-01",
        },
    }


def test_complete_protocol_passes_and_persists_its_breakdown(chain: Chain) -> None:
    result = audit(chain.hypothesis_id, protocol=_complete_protocol())

    assert result.status == "pass"
    assert result.missing_protocol_fields == []
    assert result.blocking_reasons == []
    assert set(result.checklist_results) == {
        "PROTOCOL_COMPLETENESS",
        "LOOKAHEAD_INFORMATION",
        "UNDEFINED_TIME_HORIZON",
        "UNDEFINED_UNIVERSE",
        "EXECUTION_SEMANTICS",
        "COST_MODEL_DEFINED",
        "DATA_PROVENANCE_PINNED",
        "PRIOR_FAILURE_COLLISION",
    }
    assert all(item.status == "pass" for item in result.checklist_results.values())

    # Durable, not transient: a later reader must be able to see WHICH item
    # was judged, not merely that a verdict existed.
    stored = get_latest_audit(chain.hypothesis_id)
    assert stored is not None
    assert stored.audit_id == result.audit_id
    assert stored.status == "pass"
    lookahead = stored.checklist_results["LOOKAHEAD_INFORMATION"]
    assert lookahead.status == "pass"
    assert lookahead.note

    assert [a.audit_id for a in list_audits(chain.hypothesis_id)] == [result.audit_id]


def test_empty_protocol_is_incomplete_and_never_passes(chain: Chain) -> None:
    no_protocol = audit(chain.hypothesis_id)
    empty_blocks = audit(chain.hypothesis_id, protocol={})

    for result in (no_protocol, empty_blocks):
        assert result.status == "incomplete"
        assert result.status != "pass"
        assert result.missing_protocol_fields
        assert result.blocking_reasons

    assert set(no_protocol.missing_protocol_fields) == set(empty_blocks.missing_protocol_fields)


def test_missing_one_block_is_still_incomplete(chain: Chain) -> None:
    """Completeness is all-or-nothing: one absent block blocks the whole thing.

    A partially specified protocol has no principled degree of completion, so
    "most fields are filled" is not a state this gate can act on.
    """
    protocol = _complete_protocol()
    del protocol["costs"]

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "incomplete"
    assert "costs.commission_bps" in result.missing_protocol_fields


def test_zero_is_a_defined_protocol_value(chain: Chain) -> None:
    """commission_bps = 0.0 is a decision; null is the absence of one.

    Reading falsy numbers as missing would make the honest answer look like a
    gap, which is the exact confusion the gate exists to prevent.
    """
    protocol = _complete_protocol()
    protocol["costs"]["commission_bps"] = 0.0
    protocol["execution"]["latency_ms"] = 0.0

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "pass"
    assert result.missing_protocol_fields == []


def test_same_bar_execution_is_a_hard_fail(chain: Chain) -> None:
    """Signal and execution on the same bar is look-ahead by construction."""
    protocol = _complete_protocol()
    protocol["execution"]["execution_time"] = "close of bar k"

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "fail"
    assert result.checklist_results["LOOKAHEAD_INFORMATION"].status == "fail"
    # FAIL is a rejection, not an unfinished state, so it must not be filed
    # as a recoverable gap.
    assert "LOOKAHEAD_INFORMATION" in " ".join(result.blocking_reasons)


def test_test_window_before_train_window_is_a_hard_fail(chain: Chain) -> None:
    protocol = _complete_protocol()
    protocol["temporal_protocol"]["train"] = {"start": "2020-01-01", "end": "2024-12-31"}
    protocol["temporal_protocol"]["test"] = {"start": "2022-07-01", "end": "2023-12-31"}

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "fail"
    assert result.checklist_results["LOOKAHEAD_INFORMATION"].status == "fail"


def test_unparseable_split_date_fails_closed(chain: Chain) -> None:
    """A boundary that cannot be read is a boundary that cannot be shown safe."""
    protocol = _complete_protocol()
    protocol["temporal_protocol"]["test"] = {"start": "last summer", "end": "2023-12-31"}

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "fail"
    assert result.checklist_results["LOOKAHEAD_INFORMATION"].status == "fail"


def test_unknown_survivorship_policy_is_incomplete(chain: Chain) -> None:
    protocol = _complete_protocol()
    protocol["universe"]["survivorship_policy"] = "we will look into it"

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "incomplete"
    assert result.checklist_results["UNDEFINED_UNIVERSE"].status == "incomplete"


def test_vague_horizon_fails_and_absent_horizon_is_incomplete(chain: Chain) -> None:
    vague = _complete_protocol()
    vague["temporal_protocol"]["horizon"] = "short-term"
    vague_result = audit(chain.hypothesis_id, protocol=vague)
    assert vague_result.status == "fail"
    assert vague_result.checklist_results["UNDEFINED_TIME_HORIZON"].status == "fail"

    absent = _complete_protocol()
    del absent["temporal_protocol"]["horizon"]
    absent_result = audit(chain.hypothesis_id, protocol=absent)
    assert absent_result.status == "incomplete"
    assert "temporal_protocol.horizon" in absent_result.missing_protocol_fields


def test_horizon_must_carry_a_quantity_and_a_unit(chain: Chain) -> None:
    """Either half alone is unusable.

    "4" does not say four bars or four months; "hours" does not say how many.
    A backtest forced to guess is a backtest whose result depends on a choice
    nobody recorded.
    """
    no_quantity = _complete_protocol()
    no_quantity["temporal_protocol"]["horizon"] = "hours"
    result = audit(chain.hypothesis_id, protocol=no_quantity)
    assert result.status == "fail"
    assert result.checklist_results["UNDEFINED_TIME_HORIZON"].status == "fail"

    no_unit = _complete_protocol()
    no_unit["temporal_protocol"]["horizon"] = "4"
    result = audit(chain.hypothesis_id, protocol=no_unit)
    assert result.status == "fail"
    assert result.checklist_results["UNDEFINED_TIME_HORIZON"].status == "fail"


def test_concrete_horizons_are_not_rejected(chain: Chain) -> None:
    """The gate must not fire on legitimate protocols.

    A false positive is not a safe default: it trains people to route around
    the gate, and the gate is the only thing standing between an unreproducible
    protocol and a number nobody can re-derive.
    """
    for horizon in ("4 hours", "15min", "1 session", "3 days", "20 bars later"):
        protocol = _complete_protocol()
        protocol["temporal_protocol"]["horizon"] = horizon
        result = audit(chain.hypothesis_id, protocol=protocol)
        assert result.status == "pass", f"{horizon!r} is a concrete horizon"


def test_spread_model_without_a_number_is_incomplete(chain: Chain) -> None:
    """A fixed spread carries no number until one is written down."""
    protocol = _complete_protocol()
    protocol["costs"]["spread_model"] = "fixed_bps"
    protocol["costs"]["spread_bps"] = None

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "incomplete"
    assert result.checklist_results["COST_MODEL_DEFINED"].status == "incomplete"


def test_perpetual_hypothesis_must_apply_funding(perpetual_chain: Chain) -> None:
    """Funding on a perpetual venue is a real cost and cannot be switched off."""
    protocol = _complete_protocol()
    protocol["costs"]["funding"] = "ignored"

    result = audit(perpetual_chain.hypothesis_id, protocol=protocol)

    assert result.status == "incomplete"
    assert result.checklist_results["COST_MODEL_DEFINED"].status == "incomplete"


def test_funding_rule_does_not_fire_off_perpetual_venues(chain: Chain) -> None:
    """The rule is scoped to the venue it applies to, not to every hypothesis.

    A rule that fires everywhere is a rule nobody can act on, and funding is
    genuinely ignorable where it genuinely does not exist.
    """
    protocol = _complete_protocol()
    protocol["costs"]["funding"] = "ignored"

    assert audit(chain.hypothesis_id, protocol=protocol).status == "pass"


def test_moving_dataset_version_fails(chain: Chain) -> None:
    """'latest' reads as pinned while naming a target that has already moved."""
    protocol = _complete_protocol()
    protocol["data"]["dataset_version"] = "latest"

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "fail"
    assert result.checklist_results["DATA_PROVENANCE_PINNED"].status == "fail"


def test_prior_failure_on_the_mechanism_is_a_hard_fail(chain: Chain) -> None:
    """Re-proposing a refuted mechanism must not consume a backtest slot."""
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO failures (hypothesis_id, reason, failure_type, stage, reported_by)
            VALUES (%s, 'refuted on prior run', 'future_information_leak', 'layer2_backtest',
                    'layer2.backtest')
            """,
            (chain.hypothesis_id,),
        )

    result = audit(chain.hypothesis_id, protocol=_complete_protocol())

    assert result.status == "fail"
    assert result.checklist_results["PRIOR_FAILURE_COLLISION"].status == "fail"


def test_re_audit_downgrade_withdraws_from_the_gate(chain: Chain) -> None:
    """Fail closed: the LATEST audit decides, not the latest passing one.

    An implementation that searched for the newest audit with status='pass'
    would let this hypothesis stay enqueueable after it had been found leaky,
    which is the precise failure this gate exists to prevent.
    """
    assert gate_ready(chain.hypothesis_id) is False  # never audited

    passed = audit(chain.hypothesis_id, protocol=_complete_protocol())
    assert passed.status == "pass"
    assert gate_ready(chain.hypothesis_id) is True

    downgraded = _complete_protocol()
    downgraded["execution"]["execution_time"] = "close of bar k"
    reaudited = audit(chain.hypothesis_id, protocol=downgraded)
    assert reaudited.status == "fail"

    assert get_latest_audit(chain.hypothesis_id).audit_id == reaudited.audit_id
    assert gate_ready(chain.hypothesis_id) is False

    # The earlier pass is still on the record -- it happened, and a reader
    # must be able to see it -- but it no longer speaks for this hypothesis.
    assert passed.status == "pass"
    assert len(list_audits(chain.hypothesis_id)) == 2
    assert [a.status for a in list_audits(chain.hypothesis_id)] == ["fail", "pass"]


def test_incomplete_audit_also_blocks_the_gate(chain: Chain) -> None:
    audit(chain.hypothesis_id, protocol=_complete_protocol())
    incomplete = audit(chain.hypothesis_id)

    assert incomplete.status == "incomplete"
    assert gate_ready(chain.hypothesis_id) is False


def test_audit_rejects_unknown_protocol_keys(chain: Chain) -> None:
    """Refusing is the fail-closed reading: the field we ignore is the one
    we would have failed to check."""
    protocol = _complete_protocol()
    protocol["costs"]["vibes"] = "looks fine"

    with pytest.raises(IntegrityInputError):
        audit(chain.hypothesis_id, protocol=protocol)

    assert gate_ready(chain.hypothesis_id) is False


def test_audit_rejects_unknown_hypothesis() -> None:
    with pytest.raises(IntegrityInputError):
        audit("H_does_not_exist_0000")


def test_protocol_spec_forbids_extra_keys() -> None:
    with pytest.raises(ValueError):
        ProtocolSpec.model_validate({"costs": {"commission_bps": 1.0}, "extra_block": {}})


# ---------------------------------------------------------------------
# static spec lint
# ---------------------------------------------------------------------


def _good_spec() -> dict[str, Any]:
    return {
        "hypothesis_id": "H1",
        "name": "Dispersion_Reversal",
        "source": "order flow imbalance",
        "market": {"asset_class": "crypto", "universe": "BTC/USDT perpetual"},
        "features": ["cross_sectional_dispersion"],
        "hypothesis": {
            "statement": "High dispersion predicts reversal",
            "signal": {"condition": "cross_sectional_dispersion > 2.0", "horizon": "4h"},
            "null": "no reversal conditional on high dispersion",
        },
        "expected_mechanism": "liquidity demanders absorb pressure",
    }


def test_lint_clean_spec_returns_no_findings() -> None:
    assert lint_spec(_good_spec()) == []


def test_lint_catches_a_prose_condition() -> None:
    spec = _good_spec()
    spec["hypothesis"]["signal"]["condition"] = "when the market feels unusually stressed"

    findings = lint_spec(spec)

    assert any(f.startswith("AMBIGUOUS_RULE") and "no comparison" in f for f in findings)


def test_lint_catches_a_discretionary_condition() -> None:
    spec = _good_spec()
    spec["hypothesis"]["signal"]["condition"] = "close_position if conditions suggest > 2.0"

    findings = lint_spec(spec)

    assert any(f.startswith("HIDDEN_DISCRETIONARY_DECISION") for f in findings)


def test_lint_catches_an_empty_null() -> None:
    spec = _good_spec()
    spec["hypothesis"]["null"] = ""

    findings = lint_spec(spec)

    assert any(f.startswith("UNFALSIFIABLE") for f in findings)


def test_lint_catches_empty_and_non_computable_features() -> None:
    spec = _good_spec()
    spec["features"] = ["tbd"]
    assert any(
        f.startswith("IMPOSSIBLE_TO_SOURCE_VARIABLE") for f in lint_spec(spec)
    )

    spec["features"] = []
    assert any(
        f.startswith("IMPOSSIBLE_TO_SOURCE_VARIABLE") for f in lint_spec(spec)
    )


def test_lint_catches_a_forbidden_profitability_key_at_any_depth() -> None:
    spec = _good_spec()
    spec["hypothesis"]["signal"]["expected_return"] = 0.5

    findings = lint_spec(spec)

    assert any(f.startswith("NO_PROFITABILITY_TARGET") for f in findings)


def test_lint_catches_a_forbidden_key_hidden_in_a_list() -> None:
    spec = _good_spec()
    spec["market"]["tags"] = [{"target_sharpe": 3}]

    findings = lint_spec(spec)

    assert any("target_sharpe" in f for f in findings)


def test_lint_refuses_a_non_dict() -> None:
    with pytest.raises(IntegrityInputError):
        lint_spec(["not", "a", "spec"])  # type: ignore[arg-type]


def test_checklist_item_ids_are_unique() -> None:
    """The audit row is keyed by item id, so a duplicate would silently drop one."""
    ids = [
        integrity.CHECK_PROTOCOL_COMPLETENESS,
        integrity.CHECK_LOOKAHEAD,
        integrity.CHECK_TIME_HORIZON,
        integrity.CHECK_UNIVERSE,
        integrity.CHECK_EXECUTION,
        integrity.CHECK_COSTS,
        integrity.CHECK_DATA,
        integrity.CHECK_PRIOR_FAILURE,
    ]
    assert len(set(ids)) == len(ids)


def test_no_required_field_can_be_dropped_without_blocking_the_gate(chain: Chain) -> None:
    """Exhaustive sweep: removing ANY required field must block the gate.

    The per-field tests above cover the fields whose absence has its own
    semantics.  This one covers the property itself -- it is what fails if a
    future field is added to REQUIRED_PROTOCOL_FIELDS and never wired into a
    check, which is the most likely way this gate would rot into fail-open.
    """
    for path in integrity.REQUIRED_PROTOCOL_FIELDS:
        protocol = _complete_protocol()
        block, _, field = path.partition(".")
        if field:
            del protocol[block][field]
        else:
            del protocol[block]

        result = audit(chain.hypothesis_id, protocol=protocol)

        assert result.status != "pass", f"dropping {path} must not audit to pass"
        assert path in result.missing_protocol_fields
        assert gate_ready(chain.hypothesis_id) is False


def test_fail_outranks_incomplete(chain: Chain) -> None:
    """A hard defect and an unfinished field together resolve to fail.

    The checklist's verdict_rule says the stronger verdict wins, and it has to:
    answering 'unfinished' when the protocol also leaks would send the
    hypothesis back to be re-specified when it should be rewritten.
    """
    protocol = _complete_protocol()
    protocol["execution"]["execution_time"] = "close of bar k"  # hard defect
    del protocol["data"]  # and an unfinished block

    result = audit(chain.hypothesis_id, protocol=protocol)

    assert result.status == "fail"
    assert result.checklist_results["LOOKAHEAD_INFORMATION"].status == "fail"
    assert "data.provider" in result.missing_protocol_fields


def test_protocol_spec_round_trips_a_complete_dict() -> None:
    spec = ProtocolSpec.model_validate(_complete_protocol())

    assert spec.execution is not None and spec.execution.price == "mid"
    assert spec.costs is not None and spec.costs.spread_model == "quoted"
    assert spec.universe is not None
    assert spec.universe.survivorship_policy == "delisted_retained"
    assert spec.temporal_protocol is not None
    assert spec.data is not None and spec.data.dataset_version == "2024-03-snapshot-01"