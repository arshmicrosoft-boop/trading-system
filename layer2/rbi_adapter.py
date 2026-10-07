"""The execution port.

THIS IS A PORT.  It is the single seam where an external engine attaches to
MoonBot Layer 2, and this repository deliberately implements no engine behind
it.  There is no market data here, no exchange client, no order or position
concept -- by design, because a repository that can place trades can no longer
be trusted to report an honest backtest.

Moon Dev RBI (or any other engine) attaches later by implementing
:class:`ExperimentAdapter` and calling :func:`register`.  Nothing in Layer 1
knows or cares which engine runs a job; Layer 2 only knows the three members
of the protocol.

Two decisions live here rather than in the runner:

* **Registration validates the contract.**  A duck-typed object that quacks
  wrong must fail at :func:`register`, loudly and by name, rather than
  half-way through a run that has already consumed real compute.  The error
  names the missing member.
* **The database, not this module, decides what is available.**  The
  `execution_backends` table records `is_available` and, more importantly,
  `is_real_engine`.  That table is the source of truth so an operator can look
  at one screen and see that the system has never run a real engine -- the
  single most important fact to know before trusting a number it produced.
  An adapter registered in code whose row says `is_available = false` is not
  available, however confident the object is about itself.

Nothing in `RunOutcome` is free text on the metrics path: `metrics` is a
mapping of names that must match real columns in `experiment_results`.  The
`stdout`/`stderr` fields exist for forensics only -- a consumer must never
have to parse them, because the moment a headline number lives in a log line,
it is a narrated result rather than a recorded one.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from layer1 import db

__all__ = [
    "ExperimentSpec",
    "RunOutcome",
    "ExperimentAdapter",
    "AdapterContractError",
    "UnknownBackendError",
    "register",
    "unregister",
    "get_adapter",
    "registered_backends",
    "available_backends",
    "backend_catalog",
]

#: The three members an adapter must present.  `name` is an attribute, the
#: other two are callables; a wrong shape is caught by `_validate`.
_REQUIRED_MEMBERS = ("name", "is_available", "run")


class AdapterContractError(TypeError):
    """An object was offered as an engine and is not one.

    Raised at registration, naming the member that is missing or not callable.
    The alternative -- discovering it when a run is half-executed -- wastes the
    run and leaves an orphan row in `experiment_runs` with no honest status.
    """


class UnknownBackendError(LookupError):
    """No adapter is registered under the requested name."""


class ExperimentSpec(BaseModel):
    """Everything an engine needs to run, and nothing it does not.

    The identity quadruple (`experiment_id`, `hypothesis_id`, `research_id`,
    `version_id`) is what makes the result attributable; the metadata block is
    what makes two runs comparable.  An engine receives this object and must
    not be told anything further, in particular not a profitability target:
    Layer 1 has no such number, and if one reached here it would be a bug in
    the caller rather than something to accommodate.
    """

    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    hypothesis_id: str
    research_id: str
    version_id: int

    rule_spec: dict[str, Any]

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


class RunOutcome(BaseModel):
    """What an engine reports back.

    `status` says whether the run happened; `outcome` says what the run means.
    They are separate because a run can execute perfectly and still leave the
    question unanswered -- that is `inconclusive`, and conflating it with
    `errored` would file a limitation of the evidence under "the experiment
    broke", which is a different and much more embarrassing claim.

    `metrics` keys are matched against `experiment_results` columns by name.
    A key with no matching column is not discarded: the runner moves it to
    `extra_metrics` with a note, so a number an engine produced is never
    silently lost.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    metrics: dict[str, float] = Field(default_factory=dict)
    outcome: Literal["supported", "refuted", "inconclusive", "errored"]
    extra_metrics: dict[str, Any] = Field(default_factory=dict)

    failure_type: str | None = None
    failure_reason: str | None = None

    # Forensics only. Never the record of what happened.
    stdout: str | None = None
    stderr: str | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    workspace_path: str | None = None


@runtime_checkable
class ExperimentAdapter(Protocol):
    """What an execution engine must present to be attachable."""

    name: str

    def is_available(self) -> bool:
        """Whether this engine can run right now (binary present, config sane)."""
        ...

    def run(self, spec: ExperimentSpec) -> RunOutcome:
        """Execute `spec` and report the outcome. Must not raise for a failed run."""
        ...


#: name -> adapter.  Process-local on purpose: which engine is loaded is a
#: property of the running process, not of the research database.
_REGISTRY: dict[str, ExperimentAdapter] = {}


def _validate(adapter: Any) -> str:
    """Check the protocol members and return the adapter's name.

    `isinstance(x, ExperimentAdapter)` is not enough on its own: a runtime
    protocol check verifies that attributes *exist*, not that they are the
    right kind of thing.  A class with `run = "not callable"` passes it and
    then explodes inside a run.  So the shape is checked by hand, and the error
    names what is wrong.
    """
    missing: list[str] = []
    for member in _REQUIRED_MEMBERS:
        if not hasattr(adapter, member):
            missing.append(member)
    if missing:
        raise AdapterContractError(
            f"{type(adapter).__name__} is not an ExperimentAdapter: "
            f"missing member(s) {', '.join(missing)}"
        )

    for member in ("is_available", "run"):
        if not callable(getattr(adapter, member)):
            raise AdapterContractError(
                f"{type(adapter).__name__}.{member} is not callable; "
                "an ExperimentAdapter must expose it as a method"
            )

    name = getattr(adapter, "name")
    if not isinstance(name, str) or not name.strip():
        raise AdapterContractError(
            f"{type(adapter).__name__}.name must be a non-empty string, got {name!r}"
        )
    return name.strip()


def register(adapter: ExperimentAdapter) -> None:
    """Attach an engine. Raises `AdapterContractError` if it is not one."""
    name = _validate(adapter)
    _REGISTRY[name] = adapter


def unregister(name: str) -> None:
    """Detach an engine. Raises `UnknownBackendError` if none is attached."""
    if name not in _REGISTRY:
        raise UnknownBackendError(f"no adapter registered as {name!r}")
    del _REGISTRY[name]


def registered_backends() -> list[str]:
    """Names attached in this process, regardless of whether they can run."""
    return sorted(_REGISTRY)


def get_adapter(name: str) -> ExperimentAdapter:
    """Look up an attached engine by name."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(registered_backends()) or "<none>"
        raise UnknownBackendError(
            f"no adapter registered as {name!r}; registered adapters: {known}"
        ) from None


def backend_catalog() -> list[dict[str, Any]]:
    """The `execution_backends` rows: what exists, what is usable, what is real.

    Read from the database rather than from `_REGISTRY` because the operator's
    question is not "what did someone import" but "what has this system ever
    actually run".  `is_real_engine = false` marks a backend whose numbers are
    structurally valid and evidentially worthless.
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT backend::text AS backend, description, is_available, "
            "is_real_engine, version, notes, last_checked_at "
            "FROM execution_backends ORDER BY backend"
        )
        return list(cur.fetchall())


def available_backends() -> list[str]:
    """Names that are both attached in code and flagged usable in the database.

    Both conditions are required.  The table is the source of truth: an adapter
    that believes it is ready while its row says `is_available = false` is
    describing a capability the system does not have, and a result produced
    that way would carry a confidence it has not earned.
    """
    usable = {
        row["backend"]
        for row in backend_catalog()
        if row["is_available"] and row["backend"] in _REGISTRY
    }
    return sorted(usable)