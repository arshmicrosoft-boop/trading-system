"""Tests for the adapter port.

The port is the one seam an external engine crosses, so what is tested here is
not "does an adapter work" -- there is no engine -- but whether the seam itself
refuses misuse.  A duck-typed object that quacks wrong must fail at
registration rather than half way through a run, and availability must come
from the database rather than from the adapter's own opinion of itself.
"""

from __future__ import annotations

import pytest

from layer2 import rbi_adapter
from layer2.rbi_adapter import (
    AdapterContractError,
    ExperimentSpec,
    RunOutcome,
    UnknownBackendError,
    available_backends,
    backend_catalog,
    get_adapter,
    register,
    registered_backends,
    unregister,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Leave the process registry as it was found.

    The registry is process-local and module-global, so a leaked fake adapter
    would make `available_backends()` non-deterministic for every later test.
    """
    before = dict(rbi_adapter._REGISTRY)
    yield
    rbi_adapter._REGISTRY.clear()
    rbi_adapter._REGISTRY.update(before)


class _Fake:
    """A minimal well-formed adapter."""

    name = "fake_engine"

    def is_available(self) -> bool:
        return True

    def run(self, spec: ExperimentSpec) -> RunOutcome:
        return RunOutcome(status="completed", outcome="inconclusive", metrics={"net_return": 0.0})


# ---------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------


def test_register_rejects_an_object_missing_a_member():
    class NoRun:
        name = "no_run"
        is_available = staticmethod(lambda: True)

    with pytest.raises(AdapterContractError, match="missing member"):
        register(NoRun())


def test_register_rejects_a_non_callable_member():
    class NotCallable:
        name = "bad"

        def is_available(self) -> bool:
            return True

        run = "not a method"

    with pytest.raises(AdapterContractError, match="not callable"):
        register(NotCallable())


@pytest.mark.parametrize("bad_name", ["", "   ", None, 7])
def test_register_rejects_a_missing_or_empty_name(bad_name):
    adapter = _Fake()
    adapter.name = bad_name
    with pytest.raises(AdapterContractError, match="non-empty string"):
        register(adapter)


def test_get_adapter_names_the_registered_ones_when_unknown():
    register(_Fake())
    with pytest.raises(UnknownBackendError) as exc:
        get_adapter("absent_engine")
    assert "fake_engine" in str(exc.value)


def test_unregister_unknown_raises():
    with pytest.raises(UnknownBackendError):
        unregister("absent_engine")


# ---------------------------------------------------------------------
# Availability comes from the database, not from the adapter
# ---------------------------------------------------------------------


def test_registered_but_unavailable_is_not_available():
    """`fake_engine` has no row in `execution_backends`, so it is not available.

    Code registration and operational availability are different facts. An
    adapter that has been imported is not thereby able to run, and reporting it
    as available would put a name in front of an operator that does not
    correspond to anything executable.
    """
    register(_Fake())
    assert "fake_engine" in registered_backends()
    assert "fake_engine" not in available_backends()


def test_stub_is_registered_in_the_table_but_no_stub_adapter_exists_here():
    catalog = {row["backend"]: row for row in backend_catalog()}
    assert catalog["stub"]["is_available"] is True
    assert catalog["stub"]["is_real_engine"] is False
    # Nothing in this repository implements an engine, so no code-side adapter
    # backs the stub row. This is the honest state of the system and the
    # reason the catalog is a table rather than a constant.
    assert available_backends() == []


def test_the_catalog_records_that_no_real_engine_has_run():
    """Every real engine is flagged unavailable; the stub is flagged unreal.

    This is the single most important thing to know before trusting a result,
    so it is asserted rather than left to a comment.
    """
    catalog = backend_catalog()
    assert catalog, "execution_backends must be seeded by the schema"
    for row in catalog:
        if row["is_real_engine"]:
            assert row["is_available"] is False, (
                f"{row['backend']} claims to be a real engine and available; "
                "no engine is implemented in this repository"
            )


# ---------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------


def test_run_outcome_forbids_unknown_fields():
    with pytest.raises(Exception):
        RunOutcome(status="completed", outcome="supported", net_return=0.1)


def test_experiment_spec_forbids_unknown_fields():
    with pytest.raises(Exception):
        ExperimentSpec(
            experiment_id="E",
            hypothesis_id="H",
            research_id="R",
            version_id=1,
            rule_spec={},
            target_sharpe=2.0,
        )


def test_experiment_spec_carries_no_profitability_target():
    """Layer 1 has no target and Layer 2 must not acquire one on the way.

    The fields are the controlled-experiment metadata and the rule under test.
    A profitability target has no business in either: if one appeared here it
    would be a target invented by the caller, not a criterion Layer 1 set.
    """
    spec = ExperimentSpec(
        experiment_id="E1",
        hypothesis_id="H1",
        research_id="R1",
        version_id=1,
        rule_spec={"hypothesis_id": "H1"},
    )
    assert set(spec.model_dump()) == {
        "experiment_id",
        "hypothesis_id",
        "research_id",
        "version_id",
        "rule_spec",
        "dataset",
        "dataset_version",
        "universe",
        "time_period",
        "timeframe",
        "feature_version",
        "execution_model",
        "cost_model",
        "parameters",
        "random_seed",
    }