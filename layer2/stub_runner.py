"""A deterministic no-op execution backend.  THIS IS NOT A BACKTEST ENGINE.

Every number this module produces is FABRICATED.  It has no predictive content
of any kind: it does not read a price, a bar, a fill or a dataset, because this
repository has no market data and no exchange client and must never acquire
either.  What it does produce is a STRUCTURALLY VALID `RunOutcome` -- one whose
metrics map cleanly onto the typed columns of `experiment_results` -- so that
the plumbing between "a queued experiment" and "a recorded, queryable result"
can be exercised end to end without an engine attached.

    THESE NUMBERS MUST NEVER BE PRESENTED AS EVIDENCE.

`execution_backends.is_real_engine` is False for backend 'stub' precisely so
that a reader of a result row can tell the difference at a glance.  If you find
yourself wanting to quote a stub metric, that is the failure this backend
exists to make loud rather than quiet.

The numbers are therefore NOT made to look like plausible trading results.  A
fabricated Sharpe that looks like a real Sharpe is an invitation to believe it.
The generator below emits values from a wide, unflattering range and records
the fabrication in `extra_metrics`, so a row that somehow escapes the
`is_real_engine` check still says, in its own JSONB, that it is not evidence.

Determinism is a hard requirement rather than a nicety: a test that asserts a
recorded number must be able to re-run and see the same one.  The seed is the
spec's own `random_seed` when supplied, and otherwise a digest of the spec
itself -- so one spec always yields one set of numbers, and two different
specs do not trivially collide.

Forcing the failure path: construct the adapter with `fail=True`, or pass
`outcome_override` to `run`, to get a failed `RunOutcome` without raising.  That
lets the runner's `failed` transition be exercised against a real adapter
rather than a hand-written dict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .rbi_adapter import ExperimentSpec, RunOutcome

__all__ = [
    "StubAdapter",
    "STUB_BACKEND",
    "metric_columns",
]

#: The backend name this adapter registers under.
STUB_BACKEND = "stub"

#: The `experiment_results` columns this adapter fills.  Kept as data rather
#: than as prose so a test can prove that every named headline metric lands in
#: a real column instead of in `extra_metrics`.
_METRIC_KEYS: tuple[str, ...] = (
    "gross_return",
    "net_return",
    "volatility",
    "max_drawdown",
    "sharpe",
    "sortino",
    "trade_count",
    "turnover",
    "hit_rate",
    "avg_holding_bars",
    "stability",
    "regime_dependence",
    "p_value",
    "market_exposure",
    "residual_alpha",
)

#: Every generated value carries this stamp in `extra_metrics`.  A consumer
#: that somehow reaches a stub row without checking `is_real_engine` still
#: reads the disclaimer from the row itself.
_FABRICATION_STAMP: dict[str, Any] = {
    "fabricated": True,
    "producer": "layer2.stub_runner",
    "warning": (
        "FABRICATED BY A NO-OP STUB. These numbers came from a hash of the "
        "spec, not from data. They are a plumbing test and are never evidence."
    ),
}


def metric_columns() -> tuple[str, ...]:
    """The `experiment_results` column names this adapter populates."""
    return _METRIC_KEYS


class _Stream:
    """A small deterministic pseudo-random source.

    Deliberately hand-rolled rather than `random.Random`: the algorithm is part
    of what makes a recorded stub number reproducible in six months on a
    different Python build, and pinning it here means it cannot move under the
    stdlib.  This is splitmix64 in five lines, and nothing else.
    """

    __slots__ = ("_state",)

    _MASK = (1 << 64) - 1

    def __init__(self, seed: int) -> None:
        self._state = (int(seed) ^ 0x9E3779B97F4A7C15) & self._MASK

    def _next(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & self._MASK
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self._MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self._MASK
        return z ^ (z >> 31)

    def unit(self) -> float:
        """A float in [0, 1)."""
        return (self._next() >> 11) / float(1 << 53)

    def span(self, low: float, high: float) -> float:
        """A float in [low, high)."""
        return low + (high - low) * self.unit()


def _spec_seed(spec: ExperimentSpec) -> int:
    """The deterministic seed for one spec.

    `random_seed` wins when the caller supplied one -- an explicit seed is a
    reproducibility promise and must not be silently replaced.  Otherwise the
    spec's own content is hashed, which makes the seed a function of
    everything that defines the experiment.
    """
    if spec.random_seed is not None:
        return int(spec.random_seed)
    canonical = json.dumps(
        spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:8], "big")


class StubAdapter:
    """The `ExperimentAdapter` implementation for backend 'stub'."""

    name = STUB_BACKEND

    #: Fabrication is unconditional and not configurable.  There is no flag,
    #: setting or keyword that turns these numbers into something meaningful,
    #: because that is the whole point.
    is_real_engine = False

    def __init__(self, *, fail: bool = False, failure_reason: str | None = None) -> None:
        """Build the adapter.

        Args:
            fail: every `run` returns a failed outcome instead of metrics.
            failure_reason: the reason recorded on a forced failure.
        """
        self._fail = bool(fail)
        self._failure_reason = failure_reason

    def is_available(self) -> bool:
        """Always True: a hash function is always available."""
        return True

    def run(self, spec: ExperimentSpec, *, outcome_override: str | None = None) -> RunOutcome:
        """Produce a structurally valid, meaningless outcome for `spec`.

        Args:
            spec: the experiment to pretend to run.
            outcome_override: `"failed"`/`"errored"` forces the failure path;
                anything else is ignored, because a stub has no evidence to
                support a claim it does not deserve.
        """
        if self._fail or outcome_override in {"failed", "errored"}:
            return self._failure(
                self._failure_reason
                or (
                    "stub adapter constructed with fail=True"
                    if self._fail
                    else f"stub adapter forced {outcome_override} for test purposes"
                )
            )

        rng = _Stream(_spec_seed(spec))

        gross = rng.span(-0.25, 0.35)
        volatility = rng.span(0.05, 0.60)
        # Costs are structurally positive so gross > net can never be violated
        # by chance, which would be a number the schema's own comments call a
        # marketing figure.
        net = gross - rng.span(0.0005, 0.02)

        metrics: dict[str, float] = {
            "gross_return": gross,
            "net_return": net,
            "volatility": volatility,
            "max_drawdown": rng.span(0.02, 0.45),
            "sharpe": net / volatility,
            "sortino": net / (volatility * 0.7),
            "trade_count": float(int(rng.span(0, 900))),
            "turnover": rng.span(0.1, 40.0),
            "hit_rate": rng.span(0.15, 0.75),
            "avg_holding_bars": rng.span(1.0, 96.0),
            "stability": rng.span(0.0, 1.0),
            "regime_dependence": rng.span(0.0, 1.0),
            "p_value": rng.span(0.0, 1.0),
            "market_exposure": rng.span(-1.5, 1.5),
            "residual_alpha": rng.span(-0.2, 0.2),
        }

        extra: dict[str, Any] = dict(_FABRICATION_STAMP)
        extra["stub_seed"] = _spec_seed(spec)
        extra["factor_exposure"] = {"stub_factor_a": rng.span(-1.0, 1.0)}

        return RunOutcome(
            status="completed",
            metrics=metrics,
            outcome="inconclusive",
            extra_metrics=extra,
            stdout=(
                "stub backend: no data was read and no engine ran. This line "
                "is retained for forensics only; experiment_results is the record."
            ),
            stderr=None,
            artifacts={"fabricated": True},
            workspace_path=None,
        )

    def _failure(self, reason: str) -> RunOutcome:
        """The failure outcome.  No metrics: a broken run has no result row."""
        return RunOutcome(
            status="failed",
            metrics={},
            outcome="errored",
            extra_metrics=dict(_FABRICATION_STAMP),
            failure_type="adapter_error",
            failure_reason=reason,
            stdout="stub backend: forced failure, nothing was computed.",
            stderr=reason,
        )