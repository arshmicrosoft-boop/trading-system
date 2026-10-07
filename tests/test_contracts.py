"""Guard tests for the Layer 1 contracts.

Two claims in this project are load bearing and are cheap to state but easy
to erode silently:

1. Layer 1 has no notion of a good return.  A rule spec handed to Layer 2
   must be physically unable to carry a profitability target.
2. A rule spec is content addressed, so the same research artifact always
   hashes to the same identity regardless of where it sits in the pipeline.

These tests are pure unit tests -- no database, no LLM.  They assert
observable behaviour of the models (what validates, what hashes, what
round-trips), never the shape of the source text.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml
from pydantic import ValidationError

from layer1.contracts import (
    Critique,
    HypothesisBlock,
    MarketBlock,
    RuleSpec,
    Signal,
    SourceIn,
)


# ---------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------


def _signal(condition: str = "signed_volume_imbalance_1m > 2.0") -> Signal:
    return Signal(condition=condition, horizon="15m")


def _hypothesis_block(null: str = "no forward drift conditional on imbalance") -> HypothesisBlock:
    return HypothesisBlock(
        statement="Persistent signed volume imbalance predicts drift",
        signal=_signal(),
        null=null,
    )


def _rule_spec(**overrides: object) -> RuleSpec:
    payload: dict[str, object] = {
        "hypothesis_id": "HYP_0001",
        "name": "OFI Microprice Agreement",
        "source": "Order flow imbalance and adverse selection",
        "market": MarketBlock(asset_class="crypto", universe="BTC/USDT perpetual"),
        "features": ["signed_volume_imbalance_1m", "microprice_deviation_bps"],
        "hypothesis": _hypothesis_block(),
        "expected_mechanism": "Informed liquidity providers consume imbalance before it reverses",
    }
    payload.update(overrides)
    return RuleSpec(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# 1. The boundary: no profitability target is representable
# ---------------------------------------------------------------------


def test_rulespec_rejects_expected_return_field() -> None:
    """The mechanical guarantee behind "Layer 1 has no notion of a good return".

    `extra="forbid"` means the field cannot exist, so no code path -- prompt,
    LLM hallucination, hand-written YAML -- can smuggle a target through.
    """
    with pytest.raises(ValidationError):
        _rule_spec(expected_return=0.5)


@pytest.mark.parametrize(
    "target_key",
    [
        "expected_return",
        "target_return",
        "expected_profit",
        "target_sharpe",
        "profit_target",
        "return_target",
        "expected_pnl",
    ],
)
def test_rulespec_rejects_every_profitability_spelling(target_key: str) -> None:
    """The settings-level forbidden key list is belt; extra='forbid' is braces."""
    with pytest.raises(ValidationError):
        _rule_spec(**{target_key: 0.5})


def test_hypothesis_block_rejects_expected_return() -> None:
    """The guard must hold on nested blocks too, not just the top-level spec."""
    with pytest.raises(ValidationError):
        HypothesisBlock(
            statement="Persistent imbalance predicts drift",
            signal=_signal(),
            null="no drift conditional on imbalance",
            expected_return=0.5,
        )


def test_rulespec_has_no_expected_return_attribute() -> None:
    """Even attribute access cannot reach a target: the field is not declared."""
    assert not hasattr(_rule_spec(), "expected_return")


# ---------------------------------------------------------------------
# 2. spec_sha256 is deterministic and content addressed
# ---------------------------------------------------------------------


def test_spec_hash_is_deterministic_across_instances() -> None:
    assert _rule_spec().spec_sha256() == _rule_spec().spec_sha256()


def test_spec_hash_is_stable_under_key_ordering() -> None:
    """Two specs built from reordered payloads are the same spec."""
    reordered = _rule_spec(
        expected_mechanism=(
            "Informed liquidity providers consume imbalance before it reverses"
        ),
        hypothesis=_hypothesis_block(),
        features=["signed_volume_imbalance_1m", "microprice_deviation_bps"],
        market=MarketBlock(asset_class="crypto", universe="BTC/USDT perpetual"),
        source="Order flow imbalance and adverse selection",
        name="OFI Microprice Agreement",
        hypothesis_id="HYP_0001",
    )
    assert reordered.spec_sha256() == _rule_spec().spec_sha256()


def test_spec_hash_changes_when_status_changes() -> None:
    """status is EXCLUDED from the canonical form, by design.

    A hypothesis moves proposed -> critiqued -> formalized -> queued without
    its *content* changing.  Identity is meant to follow the content only:
    if status entered the hash, re-formalizing the same rule after a status
    bump would mint a second research_id for one artifact and Layer 2 would
    run the same experiment twice.
    """
    assert _rule_spec(status="proposed").spec_sha256() == _rule_spec(
        status="queued"
    ).spec_sha256()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"hypothesis_id": "HYP_0002"}, id="hypothesis_id"),
        pytest.param({"name": "OFI Microprice Divergence"}, id="name"),
        pytest.param({"source": "A different paper"}, id="source"),
        pytest.param(
            {"market": MarketBlock(asset_class="equity", universe="ES futures")},
            id="market",
        ),
        pytest.param(
            {"features": ["signed_volume_imbalance_5m"]}, id="features"
        ),
        pytest.param(
            {"hypothesis": _hypothesis_block(null="no drift at all")}, id="hypothesis"
        ),
        pytest.param({"expected_mechanism": "Liquidity is withdrawn"}, id="mechanism"),
    ],
)
def test_spec_hash_changes_when_any_content_field_changes(
    overrides: dict[str, object],
) -> None:
    """Every content field participates in the hash -- otherwise identity is a lie."""
    assert _rule_spec(**overrides).spec_sha256() != _rule_spec().spec_sha256()


def test_spec_hash_changes_when_nested_block_changes() -> None:
    """Nesting must not hide a field from the canonical form."""
    deeper = _rule_spec(
        hypothesis=HypothesisBlock(
            statement="Persistent signed volume imbalance predicts drift",
            signal=_signal(condition="signed_volume_imbalance_1m > 9.9"),
            null="no forward drift conditional on imbalance",
        )
    )
    assert deeper.spec_sha256() != _rule_spec().spec_sha256()


def test_spec_hash_is_a_sha256_hex_digest() -> None:
    digest = _rule_spec().spec_sha256()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


# ---------------------------------------------------------------------
# 3. to_yaml round-trips
# ---------------------------------------------------------------------


def test_to_yaml_round_trips_to_the_model_dump() -> None:
    """What Layer 2 parses must equal what Layer 1 persisted.

    Any lossy step here (a coerced enum, a stringified bool, a dropped
    default) would mean the hash in the database describes a different
    document than the one a backtester actually reads.
    """
    spec = _rule_spec()
    assert yaml.safe_load(spec.to_yaml()) == spec.model_dump(mode="json")


def test_to_yaml_survives_the_name_sanitizer() -> None:
    """The sanitized identifier, not the raw human title, is what persists."""
    spec = _rule_spec()
    loaded = yaml.safe_load(spec.to_yaml())
    assert loaded["name"] == "OFI_Microprice_Agreement"
    assert spec.spec_sha256() == _rule_spec().spec_sha256()


# ---------------------------------------------------------------------
# 4. name sanitization
# ---------------------------------------------------------------------


def test_name_sanitizes_spaces_to_underscores() -> None:
    assert _rule_spec(name="OFI Microprice Agreement").name == "OFI_Microprice_Agreement"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OFI-Microprice/Agreement", "OFI_Microprice_Agreement"),
        # Interior runs are not collapsed: the sanitizer maps each character
        # independently and only strips surrounding underscores. That keeps
        # the mapping injective, so two distinct titles never collide onto
        # one identifier.
        ("  padded  name  ", "padded__name"),
        ("rule.v2", "rule_v2"),
        ("__leading_and_trailing__", "leading_and_trailing"),
        ("already_ok", "already_ok"),
    ],
)
def test_name_sanitizer_cases(raw: str, expected: str) -> None:
    assert _rule_spec(name=raw).name == expected


@pytest.mark.parametrize("raw", ["", "   ", "---", "___", "!!!", " . , "])
def test_name_without_alphanumerics_is_rejected(raw: str) -> None:
    """A nameless rule has no stable identity and no readable provenance."""
    with pytest.raises(ValidationError):
        _rule_spec(name=raw)


# ---------------------------------------------------------------------
# 5. Critique coherence
# ---------------------------------------------------------------------


def _critique(**overrides: object) -> Critique:
    payload: dict[str, object] = {
        "hypothesis_id": "HYP_0001",
        "verdict": "PASS",
        "scores": {
            "mechanism_plausible": 2,
            "measurable": 2,
            "falsifiable": 2,
            "distinct": 1,
            "no_hidden_discretion": 2,
            "no_future_leakage": 2,
            "no_prior_failure_collision": 2,
        },
        "reason": "Mechanism is stated as a flow imbalance consumed by makers.",
        "critic_model": "deterministic/v1",
        "prompt_version": "l1-v1",
    }
    payload.update(overrides)
    return Critique(**payload)  # type: ignore[arg-type]


def test_fail_verdict_requires_a_failure_type() -> None:
    """The critic cannot express a vague rejection."""
    with pytest.raises(ValidationError, match="failure_type"):
        _critique(verdict="FAIL")


def test_pass_verdict_rejects_a_failure_type() -> None:
    """PASS plus a failure_type is incoherent and would misroute the job."""
    with pytest.raises(ValidationError, match="failure_type"):
        _critique(verdict="PASS", failure_type="not_measurable")


def test_valid_fail_critique_round_trips() -> None:
    critique = _critique(
        verdict="FAIL",
        failure_type="future_information_leak",
        scores={"no_future_leakage": 0, "measurable": 2},
    )
    assert critique.verdict == "FAIL"
    assert critique.failure_type == "future_information_leak"


def test_valid_pass_critique_has_no_failure_type() -> None:
    critique = _critique()
    assert critique.verdict == "PASS"
    assert critique.failure_type is None


@pytest.mark.parametrize("score", [-1, 3, 10, 99])
def test_scores_outside_the_scale_are_rejected(score: int) -> None:
    """0/1/2 is the whole scale; a free-form score would not be comparable."""
    with pytest.raises(ValidationError, match="scores"):
        _critique(scores={"measurable": score})


def test_score_must_be_named_in_the_verdict_for_fail() -> None:
    """A FAIL with a failure_type is still checked for scale coherence."""
    with pytest.raises(ValidationError):
        _critique(
            verdict="FAIL",
            failure_type="not_distinct",
            scores={"distinct": 7},
        )


def test_critique_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        _critique(reason="")


def test_critique_rejects_unknown_failure_type() -> None:
    with pytest.raises(ValidationError):
        _critique(verdict="FAIL", failure_type="it_looked_bad_to_me")


# ---------------------------------------------------------------------
# 6. Signal requires a real condition
# ---------------------------------------------------------------------


def test_signal_rejects_empty_condition() -> None:
    with pytest.raises(ValidationError):
        _signal(condition="")


def test_signal_requires_a_condition() -> None:
    with pytest.raises(ValidationError):
        Signal(horizon="15m")


def test_signal_rejects_a_profitability_target() -> None:
    with pytest.raises(ValidationError):
        Signal(condition="x > 1", expected_return=0.5)


# ---------------------------------------------------------------------
# 7. SourceIn normalizes naive datetimes
# ---------------------------------------------------------------------


def test_source_in_normalizes_naive_datetime_to_utc() -> None:
    source = SourceIn(
        source_type="note",
        title="Microstructure note",
        content="Order flow imbalance precedes short-horizon price adjustment.",
        date=datetime(2024, 3, 1, 9, 30, 0),
    )
    assert source.date is not None
    assert source.date.tzinfo is not None
    assert source.date.utcoffset() == timezone.utc.utcoffset(None)
    assert source.date.hour == 9


def test_source_in_keeps_an_aware_datetime_offset() -> None:
    """Normalization must not silently relabel a known offset as UTC."""
    source = SourceIn(
        source_type="note",
        title="Microstructure note",
        content="Order flow imbalance precedes short-horizon price adjustment.",
        date=datetime(2024, 3, 1, 9, 30, 0, tzinfo=timezone.utc),
    )
    assert source.date is not None
    assert source.date.tzinfo is timezone.utc


def test_source_in_allows_a_missing_date() -> None:
    source = SourceIn(
        source_type="manual", title="T", content="a claim without a date"
    )
    assert source.date is None


def test_source_in_rejects_unknown_source_type() -> None:
    with pytest.raises(ValidationError):
        SourceIn(source_type="tweet", title="T", content="c")


def test_source_in_rejects_empty_content() -> None:
    with pytest.raises(ValidationError):
        SourceIn(source_type="note", title="T", content="")


# ---------------------------------------------------------------------
# 8. The refutation condition is mandatory
# ---------------------------------------------------------------------


def test_hypothesis_block_requires_null() -> None:
    with pytest.raises(ValidationError):
        HypothesisBlock(
            statement="Persistent imbalance predicts drift",
            signal=_signal(),
        )


def test_hypothesis_block_rejects_empty_null() -> None:
    with pytest.raises(ValidationError):
        _hypothesis_block(null="")


def test_rulespec_rejects_a_raw_payload_with_a_blank_null() -> None:
    """A rule with no refutation condition cannot be killed by a backtest."""
    raw = _rule_spec().model_dump(mode="json")
    raw["hypothesis"]["null"] = ""
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(raw)


def test_rulespec_rejects_a_raw_payload_missing_the_null_key() -> None:
    """A null clause is required, not merely non-empty."""
    raw = _rule_spec().model_dump(mode="json")
    del raw["hypothesis"]["null"]
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(raw)
