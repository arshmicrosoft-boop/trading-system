"""Stage 6: the Research Integrity Checker -- the gate before the queue.

This module is the Layer 1 / Layer 2 precondition.  Its output is a verdict,
and the only verdict that lets a hypothesis reach a backtest is ``pass``.

Why it exists, in one number: of the 19 primary empirical studies in the 2026
Agentic Trading survey, only 2 reported extractable time-consistent splits, 1
reported a transaction-cost model, and 1 documented survivorship handling
(``knowledge/research_integrity/reproducibility_audit.md``).  Those omissions
are not stylistic.  A protocol that does not say how the clock was set, what
the spread was, or which instruments were alive cannot be re-run by anyone,
including its author -- so the claim it supports is not falsifiable, whatever
it happens to produce.  A backtest run on an under-specified protocol reports a
number that flatters whatever free parameter the author skipped, and that
number is the defect, not the strategy.

The two rules that shape everything below:

* **INCOMPLETE is a state, not a judgement.**  A hypothesis whose protocol is
  missing a field is *unfinished research*, not a bad hypothesis.  It stays
  out of the queue, and filling the field can rescue it.  Collapsing INCOMPLETE
  into PASS is the dangerous direction: it is how a hypothesis with no cost
  model reaches a backtest and reports an unreproducible number.
* **FAIL CLOSED.**  An input this module cannot evaluate must never produce
  ``pass``.  Unparseable split dates, an unrecognised survivorship policy, a
  protocol dict carrying unknown keys -- all resolve to a non-pass verdict,
  because "the checker could not tell" and "nothing is wrong" are not the same
  statement, and only one of them is safe to act on.

The checks here are DETERMINISTIC and run in code, not in a model.  Models
reason; software computes; systems verify.  Presence-checking and date
ordering are arithmetic, and a model asked to do arithmetic will occasionally
do it wrong in the one direction that matters -- reporting that a leaky
protocol is clean.  The model-shaped half of this checklist (is the mechanism
causally plausible, is the prose ambiguous to a human reader) is handled by the
Research Critic; :func:`lint_spec` covers the part that is decidable from the
spec text alone.

**No profitability target appears anywhere in this module, and none may.**  The
gate judges research rigour: is the mechanism stated, is it falsifiable, is it
temporally honest, is it measurable.  It never asks whether the idea makes
money, because Layer 1 has no target to optimise and must not be able to see
one.  A research layer that can see a profitability target generates
hypotheses tuned to flatter a backtest rather than hypotheses that are true.
Layer 2 decides profitability; a hypothesis can pass every item here and still
lose money, which is the correct order of concerns.

The audit is a DURABLE RECORD, not a transient check.  Every verdict is written
to ``integrity_audits`` with its per-item breakdown, so a later reader can see
*which* item failed and not merely that one did -- and so that "the gate was
consulted" is a queryable fact rather than a claim about a process.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from . import db
from .config import get_settings

__all__ = [
    "TemporalSplit",
    "TemporalProtocol",
    "Universe",
    "Execution",
    "Costs",
    "DataBlock",
    "ProtocolSpec",
    "ChecklistItem",
    "IntegrityResult",
    "IntegrityInputError",
    "REQUIRED_PROTOCOL_FIELDS",
    "SURVIVORSHIP_POLICIES",
    "audit",
    "get_latest_audit",
    "list_audits",
    "gate_ready",
    "lint_spec",
]

IntegrityStatus = Literal["pass", "incomplete", "fail"]


class IntegrityInputError(ValueError):
    """The checker was handed something it cannot audit at all.

    Raised only for input that is malformed as input -- an unknown protocol
    key, a protocol that is not an object, a hypothesis id that does not
    exist.  A *well-formed* protocol with gaps is not an error: that is an
    ``incomplete`` verdict, and the whole point of distinguishing the two is
    that an incomplete protocol is recoverable while a malformed one is a bug
    in the caller.
    """


# ---------------------------------------------------------------------
# Protocol model -- the five blocks of reproducibility_audit.md
# ---------------------------------------------------------------------
#
# Every field is optional at the Pydantic level.  This is a deliberate
# inversion of the usual instinct: if the model rejected a missing field,
# `audit()` would raise instead of REPORTING that the field is missing, and a
# field that cannot be reported can never be filled in.  The auditor's job is
# to make the gap legible, so the model has to be constructible in the exact
# shape that gap takes.
#
# extra="forbid" is the opposite decision and is also deliberate.  An unknown
# key means the caller's protocol and this module's protocol have diverged --
# maybe the field it wrote is one we silently do not check.  Refusing to
# evaluate a protocol we do not fully understand is the fail-closed reading;
# ignoring the key would fail OPEN by omission.


class TemporalSplit(BaseModel):
    """One time range of a data split.

    Dates are kept as strings rather than ``date`` because the audit must be
    able to *report* a boundary it could not parse as a defect; coercing at
    parse time would raise instead of recording.
    """

    model_config = ConfigDict(extra="forbid")

    start: str | None = None
    end: str | None = None


class TemporalProtocol(BaseModel):
    """How the clock was set. Answers the survey's 2-of-19 split finding.

    ``horizon`` is not in the YAML block in reproducibility_audit.md, but the
    checklist's UNDEFINED_TIME_HORIZON item makes it mandatory and Layer 1
    records it as undefined rather than letting Layer 2 pick one after seeing
    the data.  A horizon chosen downstream is a horizon fitted to the result.
    """

    model_config = ConfigDict(extra="forbid")

    train: TemporalSplit | None = None
    validation: TemporalSplit | None = None
    test: TemporalSplit | None = None
    horizon: str | None = None
    split_type: str | None = None
    purge_embargo_hours: float | None = None


class Universe(BaseModel):
    """What was in the sample. Answers the survey's 1-of-19 survivorship finding.

    ``assets`` and ``inclusion_rule`` are alternatives, not both: an explicit
    instrument list or a documented selection rule, either of which pins the
    universe.  ``survivorship_policy`` is separate and mandatory, because
    naming the assets says nothing about what happened to the ones that died.
    """

    model_config = ConfigDict(extra="forbid")

    assets: list[str] | None = None
    inclusion_rule: str | None = None
    survivorship_policy: str | None = None
    quote_currency: str | None = None
    notes: str | None = None


class Execution(BaseModel):
    """When the signal became an order, and at what price. Answers 11-of-19.

    This block is not administrative.  An execution assumption that is missing
    makes every downstream cost estimate meaningless: there is no way to know
    whether a spread was crossed, and "gross" and "net" collapse into the same
    number.
    """

    model_config = ConfigDict(extra="forbid")

    signal_time: str | None = None
    execution_time: str | None = None
    price: str | None = None
    latency_ms: float | None = None
    timezone: str | None = None


class Costs(BaseModel):
    """The transaction-cost model. Answers the survey's 1-of-19 cost finding.

    ``spread_bps`` and ``slippage_bps`` are nullable BY DESIGN: the contract
    makes them required only under the models that need a number, and a
    default would defeat the purpose -- a default is a free parameter in
    disguise, letting the author skip a choice while still consuming it.
    """

    model_config = ConfigDict(extra="forbid")

    commission_bps: float | None = None
    spread_model: str | None = None
    spread_bps: float | None = None
    slippage_model: str | None = None
    slippage_bps: float | None = None
    funding: str | None = None
    funding_interval_hours: float | None = None


class DataBlock(BaseModel):
    """Which data, pinned.

    Layer 1 owns no datasets.  It records the pointer and refuses to queue a
    hypothesis until someone who does own the data supplies it; the honest
    state is "undefined", and it is loud.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    dataset_version: str | None = None
    timezone: str | None = None
    timestamped_at: str | None = None


class ProtocolSpec(BaseModel):
    """The five-block experimental protocol.

    Deliberately NOT the same shape as ``RuleSpec``.  A RuleSpec is the rule;
    this is the experiment that would test it, and the second has five blocks
    the first has no field for at all.  Merging them would mean a spec that
    looked complete while silently carrying no cost model.
    """

    model_config = ConfigDict(extra="forbid")

    temporal_protocol: TemporalProtocol | None = None
    universe: Universe | None = None
    execution: Execution | None = None
    costs: Costs | None = None
    data: DataBlock | None = None


#: The protocol fields whose absence makes an experiment non-reproducible.
#:
#: Dotted paths, resolved against a ``ProtocolSpec``.  The list is the
#: operational form of the hard rule in reproducibility_audit.md: the contract
#: is all-or-nothing, because a partially specified protocol has no principled
#: degree of completion.
REQUIRED_PROTOCOL_FIELDS: tuple[str, ...] = (
    "temporal_protocol.train",
    "temporal_protocol.test",
    "temporal_protocol.horizon",
    "universe",
    "universe.survivorship_policy",
    "execution.signal_time",
    "execution.execution_time",
    "execution.price",
    "execution.latency_ms",
    "costs.commission_bps",
    "costs.spread_model",
    "costs.slippage_model",
    "costs.funding",
    "data.provider",
    "data.dataset_version",
)

#: ``universe`` is satisfied by EITHER an explicit list or a selection rule.
#: Modelling the disjunction here rather than in ``REQUIRED_PROTOCOL_FIELDS``
#: keeps that tuple a flat, machine-readable list of field paths.
_SATISFIED_BY: dict[str, tuple[str, ...]] = {
    "universe": ("universe.assets", "universe.inclusion_rule"),
}

#: The enumerated survivorship policies. An unrecognised value is not a policy
#: anyone will implement, so it counts as unaddressed rather than addressed.
SURVIVORSHIP_POLICIES: tuple[str, ...] = (
    "delisted_retained",
    "delisted_excluded",
    "point_in_time_index",
)

#: Tokens that name no data snapshot. "latest" is a moving target: the result
#: cannot be re-derived later because the thing that produced it no longer
#: exists under that name.
_NON_SNAPSHOT_VERSIONS = frozenset({"latest", "current", "live", "rolling", "unknown", "n/a"})

#: Spread models that obtain their number from the tape instead of a constant.
_QUOTED_SPREAD_MODELS = frozenset({"quoted"})

#: Slippage models that are either explicitly absent or derived from volume.
_NUMERIC_SLIPPAGE_MODELS = frozenset({"none", "volume_impact"})

#: Horizon descriptors that admit any window at all, from the checklist's
#: horizon_parse rule.  Deliberately excludes bare "later"/"soon": those
#: collide with concrete horizons like "4 bars later", and a false positive on
#: a legitimate protocol trains people to route around the gate.
_VAGUE_HORIZON = (
    "eventually",
    "next time",
    "short-term",
    "short term",
    "long-term",
    "long term",
    "medium-term",
    "medium term",
    "near term",
    "over time",
    "at some point",
)

#: Units that make a horizon resolvable against a bar series.  The checklist
#: requires explicit units precisely because "1 day" is ambiguous between 24h
#: and the venue's daily boundary -- crypto trades continuously.
_HORIZON_UNITS = (
    "bar",
    "min",
    "hour",
    "session",
    "day",
    "week",
    "month",
    "tick",
    "second",
)

_HAS_DIGIT = re.compile(r"\d")

#: Hypotheses about these venues trade a cost that cannot be switched off.
#: reproducibility_audit.md rule 4: a perpetual-futures mechanism without
#: funding applied is INCOMPLETE by rule, not by preference.
_PERPETUAL_LEXICON = ("perpetual", "perp ", "funding", "liquidation")

#: Used by `_looks_like_bar_ref` to detect "close of bar k" style references.
_BAR_REF = re.compile(r"bar\s*k?\s*(\d+)", re.IGNORECASE)

#: Checklist item ids. These are the checks this module decides in code; the
#: nine-item checklist in RESEARCH_INTEGRITY_CHECKLIST.md is implemented by
#: this module, the critic, and `lint_spec` together, and the mapping is
#: documented on each check below.
CHECK_PROTOCOL_COMPLETENESS = "PROTOCOL_COMPLETENESS"
CHECK_LOOKAHEAD = "LOOKAHEAD_INFORMATION"
CHECK_TIME_HORIZON = "UNDEFINED_TIME_HORIZON"
CHECK_UNIVERSE = "UNDEFINED_UNIVERSE"
CHECK_EXECUTION = "EXECUTION_SEMANTICS"
CHECK_COSTS = "COST_MODEL_DEFINED"
CHECK_DATA = "DATA_PROVENANCE_PINNED"
CHECK_PRIOR_FAILURE = "PRIOR_FAILURE_COLLISION"


# ---------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------


class ChecklistItem(BaseModel):
    """One checklist line and what became of it.

    ``note`` is written for a human reading the row in six months, because
    the durable record is only useful if the person who has to act on it can
    tell what to fix without re-running the audit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: IntegrityStatus
    note: str


class IntegrityResult(BaseModel):
    """The verdict, as stored.

    ``audit_id`` is present on a returned result and absent from nothing:
    `audit()` always persists, so there is no in-memory verdict that has no
    durable counterpart.
    """

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    hypothesis_id: str
    status: IntegrityStatus
    checklist_results: dict[str, ChecklistItem] = Field(default_factory=dict)
    missing_protocol_fields: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    audited_at: datetime


# ---------------------------------------------------------------------
# Presence helpers
# ---------------------------------------------------------------------


def _defined(value: Any) -> bool:
    """Whether a protocol field is concretely specified.

    Zero is defined.  ``commission_bps: 0.0`` is a deliberate, defensible
    statement about a venue with no commission; ``commission_bps: null`` is
    nobody having decided.  Treating falsy numbers as missing would make the
    honest answer read as a gap, and the whole contract is about telling those
    two apart.

    Empty containers and blank strings are not defined: there is no reading of
    ``assets: []`` in which the universe has been pinned down.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return len(value) > 0
    return True


def _resolve(protocol: ProtocolSpec, path: str) -> Any:
    """Walk a dotted path, returning None at the first absent or null block."""
    node: Any = protocol
    for part in path.split("."):
        if node is None:
            return None
        node = getattr(node, part, None)
    return node


def _missing_required_fields(protocol: ProtocolSpec) -> list[str]:
    """Required paths whose alternatives are all undefined, in declaration order."""
    missing: list[str] = []
    for path in REQUIRED_PROTOCOL_FIELDS:
        alternatives = _SATISFIED_BY.get(path, (path,))
        if not any(_defined(_resolve(protocol, alt)) for alt in alternatives):
            missing.append(path)
    return missing


def _parse_date(value: str | None) -> date | None:
    """Parse an ISO date, or None -- including when the string is unparseable.

    The two cases are kept together because they lead to the same safe
    outcome: a boundary this module cannot resolve is a boundary it cannot
    prove does not leak.
    """
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------


def _check_protocol_completeness(protocol: ProtocolSpec) -> ChecklistItem:
    """Checklist item: which required protocol fields are undefined.

    Purely presence-based and therefore purely mechanical; it is separated
    from the semantic checks below so that "you forgot a block" is reported as
    its own line rather than as a side effect of a leak finding.
    """
    missing = _missing_required_fields(protocol)
    if missing:
        return ChecklistItem(
            id=CHECK_PROTOCOL_COMPLETENESS,
            status="incomplete",
            note="undefined protocol fields: " + ", ".join(missing),
        )
    return ChecklistItem(
        id=CHECK_PROTOCOL_COMPLETENESS,
        status="pass",
        note=f"all {len(REQUIRED_PROTOCOL_FIELDS)} required protocol fields are specified",
    )


def _check_lookahead(protocol: ProtocolSpec) -> ChecklistItem:
    """Checklist item LOOKAHEAD_INFORMATION: is future information reachable?

    Two distinct verdicts, per the checklist.  A split that lets training see
    test, or a signal and an execution both resolved to the same bar, is a
    hard FAIL -- the protocol is actively wrong, not merely unfinished.  A
    missing temporal block is INCOMPLETE, and the distinction is load-bearing:
    INCOMPLETE is recoverable by writing the field down, FAIL is a rejection.
    """
    temporal = protocol.temporal_protocol
    execu = protocol.execution

    signal_time = (execu.signal_time or "").strip() if execu else ""
    execution_time = (execu.execution_time or "").strip() if execu else ""

    # Same-bar ordering is the checklist's automatic FAIL: computing a signal
    # from bar k's close and trading at bar k's close is look-ahead by
    # construction, not by accident.
    if signal_time and execution_time:
        if " ".join(signal_time.lower().split()) == " ".join(execution_time.lower().split()):
            return ChecklistItem(
                id=CHECK_LOOKAHEAD,
                status="fail",
                note=(
                    f"signal_time and execution_time are both {signal_time!r}: the rule "
                    "reads information from the same bar it trades on"
                ),
            )
        signal_bar = _BAR_REF.search(signal_time)
        execution_bar = _BAR_REF.search(execution_time)
        if signal_bar and execution_bar and signal_bar.group(1) == execution_bar.group(1):
            return ChecklistItem(
                id=CHECK_LOOKAHEAD,
                status="fail",
                note=(
                    f"signal_time resolves to bar {signal_bar.group(1)} and execution_time "
                    "to the same bar; execution must be strictly later"
                ),
            )

    if temporal is None:
        return ChecklistItem(
            id=CHECK_LOOKAHEAD,
            status="incomplete",
            note="no temporal protocol: the clock was never set, so no split can be verified",
        )

    edges = [
        ("train.end", temporal.train.end if temporal.train else None),
        ("validation.start", temporal.validation.start if temporal.validation else None),
        ("validation.end", temporal.validation.end if temporal.validation else None),
        ("test.start", temporal.test.start if temporal.test else None),
        ("test.end", temporal.test.end if temporal.test else None),
    ]

    parsed: dict[str, date] = {}
    for name, raw in edges:
        if raw is None or not raw.strip():
            continue
        value = _parse_date(raw)
        if value is None:
            return ChecklistItem(
                id=CHECK_LOOKAHEAD,
                status="fail",
                note=f"{name}={raw!r} is not an ISO date, so the split cannot be shown non-overlapping",
            )
        parsed[name] = value

    # Only boundaries that were actually written are compared.  train.end <=
    # validation.start <= validation.end <= test.start <= test.end is the
    # order the checklist names; a validation block that was skipped leaves a
    # gap in the chain and the remaining pairs are still checked.
    ordering = [
        ("train.end", "validation.start"),
        ("validation.start", "validation.end"),
        ("validation.end", "test.start"),
        ("test.start", "test.end"),
    ]
    for earlier, later in ordering:
        if earlier in parsed and later in parsed and parsed[earlier] > parsed[later]:
            return ChecklistItem(
                id=CHECK_LOOKAHEAD,
                status="fail",
                note=(
                    f"{earlier}={parsed[earlier]} is after {later}={parsed[later]}: "
                    "the earlier split contains data the later one must not have"
                ),
            )

    if not _defined(temporal.test):
        return ChecklistItem(
            id=CHECK_LOOKAHEAD,
            status="incomplete",
            note="temporal_protocol.test is undefined: there is no held-out period to judge on",
        )

    if temporal.purge_embargo_hours is not None and temporal.purge_embargo_hours < 0:
        return ChecklistItem(
            id=CHECK_LOOKAHEAD,
            status="fail",
            note=(
                f"purge_embargo_hours={temporal.purge_embargo_hours} is negative: overlapping "
                "labels would leak across the split boundary"
            ),
        )

    return ChecklistItem(
        id=CHECK_LOOKAHEAD,
        status="pass",
        note="no same-bar execution and no overlapping split boundary",
    )


def _check_horizon(protocol: ProtocolSpec) -> ChecklistItem:
    """Checklist item UNDEFINED_TIME_HORIZON.

    Undefined is INCOMPLETE -- the field exists, nobody filled it.  Present but
    vague is FAIL: a horizon like "short-term" admits every window in which the
    claim happened to be right, so the prediction cannot come out wrong and
    cannot be falsified.
    """
    temporal = protocol.temporal_protocol
    horizon = (temporal.horizon or "").strip() if temporal else ""

    if not _defined(horizon):
        return ChecklistItem(
            id=CHECK_TIME_HORIZON,
            status="incomplete",
            note="temporal_protocol.horizon is undefined: a prediction with no horizon is unfalsifiable",
        )

    lowered = horizon.lower()
    for phrase in _VAGUE_HORIZON:
        if phrase in lowered:
            return ChecklistItem(
                id=CHECK_TIME_HORIZON,
                status="fail",
                note=f"horizon {horizon!r} is vague ({phrase!r}); no window can refute it",
            )

    # A horizon must be numeric AND carry a unit.  Either alone is unusable:
    # "4" does not say whether that is four bars or four months, and "hours"
    # does not say how many.  Requiring both is what makes the measurement in
    # this checklist possible at all.
    if not _HAS_DIGIT.search(horizon):
        return ChecklistItem(
            id=CHECK_TIME_HORIZON,
            status="fail",
            note=f"horizon {horizon!r} carries no quantity; it cannot be measured",
        )
    if not any(unit in lowered for unit in _HORIZON_UNITS):
        return ChecklistItem(
            id=CHECK_TIME_HORIZON,
            status="fail",
            note=(
                f"horizon {horizon!r} has no unit (expected one of "
                f"{', '.join(_HORIZON_UNITS)}); bars and wall clock are not interchangeable"
            ),
        )

    return ChecklistItem(
        id=CHECK_TIME_HORIZON,
        status="pass",
        note=f"horizon {horizon!r} is numeric and carries a unit",
    )


def _check_universe(protocol: ProtocolSpec) -> ChecklistItem:
    """Checklist item UNDEFINED_UNIVERSE.

    There is no FAIL variant here and never will be: the instrument list is a
    decision Layer 1 does not own, and an undecided universe is unfinished
    research rather than a defective hypothesis.
    """
    universe = protocol.universe
    if universe is None:
        return ChecklistItem(
            id=CHECK_UNIVERSE,
            status="incomplete",
            note="no universe block: which instruments were tradeable is unstated",
        )

    if not _defined(universe.assets) and not _defined(universe.inclusion_rule):
        return ChecklistItem(
            id=CHECK_UNIVERSE,
            status="incomplete",
            note="universe.assets and universe.inclusion_rule are both undefined",
        )

    policy = (universe.survivorship_policy or "").strip()
    if not policy:
        return ChecklistItem(
            id=CHECK_UNIVERSE,
            status="incomplete",
            note=(
                "universe.survivorship_policy is undefined: the assets that died are the "
                "whole question, and naming the survivors says nothing about them"
            ),
        )
    if policy not in SURVIVORSHIP_POLICIES:
        return ChecklistItem(
            id=CHECK_UNIVERSE,
            status="incomplete",
            note=(
                f"survivorship_policy={policy!r} is not one of "
                f"{', '.join(SURVIVORSHIP_POLICIES)}"
            ),
        )

    return ChecklistItem(
        id=CHECK_UNIVERSE,
        status="pass",
        note=f"universe pinned with survivorship_policy={policy}",
    )


def _check_execution(protocol: ProtocolSpec) -> ChecklistItem:
    """Execution semantics present and ordered.

    Covers the checklist's ordering rule and its unfrozen-parameter rule in the
    place they bite: an execution block that does not say when, or at what
    price, leaves the cost model and the signal unconnected.
    """
    execu = protocol.execution
    if execu is None:
        return ChecklistItem(
            id=CHECK_EXECUTION,
            status="incomplete",
            note="no execution block: no way to know whether the spread was crossed",
        )

    absent = [
        name
        for name in ("signal_time", "execution_time", "price", "latency_ms")
        if not _defined(getattr(execu, name))
    ]
    if absent:
        return ChecklistItem(
            id=CHECK_EXECUTION,
            status="incomplete",
            note="undefined execution fields: " + ", ".join(absent),
        )

    return ChecklistItem(
        id=CHECK_EXECUTION,
        status="pass",
        note=f"signal {execu.signal_time!r} -> execution {execu.execution_time!r} at {execu.price!r}",
    )


def _check_costs(protocol: ProtocolSpec, hypothesis_text: str) -> ChecklistItem:
    """Checklist item: the transaction-cost model.

    ``spread_bps`` and ``slippage_bps`` are required only under the models that
    need a number -- a quoted spread supplies its own, and an explicit
    ``slippage_model: none`` is a decision rather than an omission.  What is
    never allowed is the model being absent, because then gross and net are
    the same number and the claim is unfalsifiable.
    """
    costs = protocol.costs
    if costs is None:
        return ChecklistItem(
            id=CHECK_COSTS,
            status="incomplete",
            note="no costs block: gross and net are the same number, so nothing is falsifiable",
        )

    spread_model = (costs.spread_model or "").strip().lower()
    slippage_model = (costs.slippage_model or "").strip().lower()
    funding = (costs.funding or "").strip().lower()

    if spread_model and spread_model not in _QUOTED_SPREAD_MODELS and not _defined(costs.spread_bps):
        return ChecklistItem(
            id=CHECK_COSTS,
            status="incomplete",
            note=f"costs.spread_model={spread_model!r} requires costs.spread_bps",
        )

    if slippage_model and slippage_model not in _NUMERIC_SLIPPAGE_MODELS and not _defined(
        costs.slippage_bps
    ):
        return ChecklistItem(
            id=CHECK_COSTS,
            status="incomplete",
            note=f"costs.slippage_model={slippage_model!r} requires costs.slippage_bps",
        )

    lowered_text = hypothesis_text.lower()
    if any(token in lowered_text for token in _PERPETUAL_LEXICON) and funding != "applied":
        return ChecklistItem(
            id=CHECK_COSTS,
            status="incomplete",
            note=(
                "hypothesis concerns perpetual-futures funding/liquidation but "
                f"costs.funding={(funding or 'undefined')!r}; funding on a perpetual venue "
                "is a real cost and cannot be switched off"
            ),
        )

    return ChecklistItem(
        id=CHECK_COSTS,
        status="pass",
        note=f"costs specified (spread={spread_model}, slippage={slippage_model}, funding={funding or 'n/a'})",
    )


def _check_data(protocol: ProtocolSpec) -> ChecklistItem:
    """Data provenance, or an explicit record that there is none.

    Layer 1 does not own datasets and must not invent one.  It records the
    pointer as undefined and refuses to queue.  A version string naming a
    moving target is worse than an absent one, because it reads as pinned.
    """
    data = protocol.data
    if data is None:
        return ChecklistItem(
            id=CHECK_DATA,
            status="incomplete",
            note="no data block: the result cannot be re-derived because nothing is pinned",
        )

    version = (data.dataset_version or "").strip().lower()
    if version and version in _NON_SNAPSHOT_VERSIONS:
        return ChecklistItem(
            id=CHECK_DATA,
            status="fail",
            note=(
                f"data.dataset_version={data.dataset_version!r} names a moving target; "
                "a snapshot id is required for the result to be reproducible"
            ),
        )

    absent = [
        name
        for name in ("provider", "dataset_version")
        if not _defined(getattr(data, name))
    ]
    if absent:
        return ChecklistItem(
            id=CHECK_DATA,
            status="incomplete",
            note="undefined data fields: " + ", ".join(absent),
        )

    return ChecklistItem(
        id=CHECK_DATA,
        status="pass",
        note=f"data pinned to {data.provider}/{data.dataset_version}",
    )


def _prior_failures(hypothesis_id: str, mechanism_id: str) -> list[str]:
    """Failures already recorded against this hypothesis or its mechanism.

    The checklist counts a collision with the mechanism's recorded failures as
    its own failure type, separate from a plain duplicate: re-proposing a
    refuted mechanism is the generator ignoring its own evidence, and it is how
    a research program spends its budget re-testing the same idea.
    """
    with db.tx() as cur:
        cur.execute(
            """
            SELECT f.failure_id, f.hypothesis_id, f.failure_type, left(f.reason, 200) AS reason
            FROM failures f
            JOIN hypotheses h ON h.hypothesis_id = f.hypothesis_id
            WHERE f.hypothesis_id = %s OR h.mechanism_id = %s
            ORDER BY f.created_at DESC, f.failure_id DESC
            """,
            (hypothesis_id, mechanism_id),
        )
        return [
            f"{row['failure_id']}:{row['failure_type']}:{row['reason']}" for row in cur.fetchall()
        ]


def _check_prior_failure(hypothesis_id: str, mechanism_id: str) -> ChecklistItem:
    """Checklist item DUPLICATE_HYPOTHESIS, failures-collision branch.

    Fails closed: the candidate carries nothing that demonstrates it addresses
    the recorded reason, so treating an unexplained collision as acceptable
    would assume the collision resolves itself. It does not.
    """
    collisions = _prior_failures(hypothesis_id, mechanism_id)
    if collisions:
        return ChecklistItem(
            id=CHECK_PRIOR_FAILURE,
            status="fail",
            note=(
                f"{len(collisions)} recorded failure(s) already bear on this hypothesis or its "
                f"mechanism, and nothing here addresses them: {'; '.join(collisions[:3])}"
            ),
        )
    return ChecklistItem(
        id=CHECK_PRIOR_FAILURE,
        status="pass",
        note="no recorded failure for this hypothesis or its mechanism",
    )


# ---------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------


def _verdict(items: list[ChecklistItem]) -> IntegrityStatus:
    """Fold per-item verdicts into the audit status.

    FAIL beats INCOMPLETE, exactly as ``verdict_rule`` in the checklist
    specifies.  The stronger verdict wins because "this is wrong" and "this is
    unfinished" call for different responses, and answering the second when the
    first is true would waste a research cycle re-filling fields on a
    hypothesis that has to be rewritten.
    """
    if any(item.status == "fail" for item in items):
        return "fail"
    if any(item.status == "incomplete" for item in items):
        return "incomplete"
    return "pass"


def _blocking_reasons(items: list[ChecklistItem]) -> list[str]:
    return [f"{item.id}: {item.note}" for item in items if item.status != "pass"]


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------


def _coerce_protocol(protocol: ProtocolSpec | dict[str, Any] | None) -> ProtocolSpec:
    if protocol is None:
        # No protocol supplied is the honest reading of "we have not written
        # one yet", and it must be auditable -- that hypothesis is exactly the
        # one the gate exists to catch.
        return ProtocolSpec()
    if isinstance(protocol, ProtocolSpec):
        return protocol
    if not isinstance(protocol, dict):
        raise IntegrityInputError(
            f"protocol must be a ProtocolSpec, a dict, or None, not {type(protocol).__name__}"
        )
    try:
        return ProtocolSpec.model_validate(protocol)
    except ValueError as exc:
        raise IntegrityInputError(
            "protocol carries keys this checker does not understand; refusing to "
            f"evaluate a protocol that may hide an unchecked field: {exc}"
        ) from exc


def _load_hypothesis_row(hypothesis_id: str) -> dict[str, Any]:
    """The hypothesis record, needed for the FK and for the funding rule.

    Read in its own transaction rather than joined into the audit write: the
    audit must not be inserted against a hypothesis that has since been
    deleted, and the error is a caller bug worth naming rather than a
    constraint violation to swallow.
    """
    with db.tx() as cur:
        cur.execute(
            """
            SELECT hypothesis_id, mechanism_id, statement, expected_mechanism
            FROM hypotheses
            WHERE hypothesis_id = %s
            """,
            (hypothesis_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise IntegrityInputError(f"unknown hypothesis {hypothesis_id!r}")
    return dict(row)


def _persist(
    hypothesis_id: str,
    status: IntegrityStatus,
    items: list[ChecklistItem],
    missing: list[str],
    blocking: list[str],
) -> IntegrityResult:
    audit_id = f"INT_{uuid4().hex[:12]}"
    checklist = {item.id: item.model_dump(mode="json") for item in items}
    with db.tx() as cur:
        cur.execute(
            """
            INSERT INTO integrity_audits
                (audit_id, hypothesis_id, status, checklist_results,
                 missing_protocol_fields, blocking_reasons)
            VALUES (%s, %s, %s::integrity_status, %s, %s, %s)
            RETURNING audited_at
            """,
            (audit_id, hypothesis_id, status, db.Jsonb(checklist), missing, blocking),
        )
        audited_at = cur.fetchone()["audited_at"]
    return IntegrityResult(
        audit_id=audit_id,
        hypothesis_id=hypothesis_id,
        status=status,
        checklist_results={item.id: item for item in items},
        missing_protocol_fields=missing,
        blocking_reasons=blocking,
        audited_at=audited_at,
    )


def _row_to_result(row: dict[str, Any]) -> IntegrityResult:
    raw = row.get("checklist_results") or {}
    return IntegrityResult(
        audit_id=row["audit_id"],
        hypothesis_id=row["hypothesis_id"],
        status=row["status"],
        checklist_results={k: ChecklistItem.model_validate(v) for k, v in raw.items()},
        missing_protocol_fields=list(row.get("missing_protocol_fields") or []),
        blocking_reasons=list(row.get("blocking_reasons") or []),
        audited_at=row["audited_at"],
    )


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def audit(
    hypothesis_id: str, *, protocol: ProtocolSpec | dict[str, Any] | None = None
) -> IntegrityResult:
    """Run the checklist against one hypothesis and record the verdict.

    Every call persists.  The audit is the record of what was known when the
    hypothesis stood at the queue boundary, and a gate whose verdicts vanish
    with the process leaves no way to answer "was this ever checked?" later.

    The checks are deterministic and run in code.  No model is consulted:
    presence-checking and date ordering are arithmetic, and this gate exists
    precisely because the cost of being wrong in one direction -- reporting a
    leaky protocol as clean -- is the entire downside of the layer.

    Raises ``IntegrityInputError`` only for malformed *input* (unknown
    protocol keys, unknown hypothesis).  A well-formed protocol with gaps is
    not an error; it is an ``incomplete`` verdict, which is the recoverable
    state this module is built to represent.
    """
    spec = _coerce_protocol(protocol)
    row = _load_hypothesis_row(hypothesis_id)
    hypothesis_text = f"{row['statement']} {row['expected_mechanism']}"

    items = [
        _check_protocol_completeness(spec),
        _check_lookahead(spec),
        _check_horizon(spec),
        _check_universe(spec),
        _check_execution(spec),
        _check_costs(spec, hypothesis_text),
        _check_data(spec),
        _check_prior_failure(hypothesis_id, row["mechanism_id"]),
    ]

    status = _verdict(items)
    missing = _missing_required_fields(spec)
    blocking = _blocking_reasons(items)
    return _persist(hypothesis_id, status, items, missing, blocking)


def get_latest_audit(hypothesis_id: str) -> IntegrityResult | None:
    """The newest audit of this hypothesis, whatever its status, or None.

    "Whatever its status" is the whole point.  A reader asking what the gate
    currently believes must not be handed an older, more flattering verdict
    because the newest one is inconvenient.
    """
    with db.tx() as cur:
        cur.execute(
            """
            SELECT audit_id, hypothesis_id, status, checklist_results,
                   missing_protocol_fields, blocking_reasons, audited_at
            FROM integrity_audits
            WHERE hypothesis_id = %s
            ORDER BY audited_at DESC, audit_id DESC
            LIMIT 1
            """,
            (hypothesis_id,),
        )
        row = cur.fetchone()
    return _row_to_result(dict(row)) if row is not None else None


def list_audits(
    hypothesis_id: str | None = None,
    status: IntegrityStatus | None = None,
    limit: int = 100,
) -> list[IntegrityResult]:
    """Stored audits, newest first, optionally filtered.

    Reads the durable record rather than re-deriving a verdict: an audit is
    evidence about a moment, and re-running the checks would answer a
    different question than the one the reader asked.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if hypothesis_id is not None:
        clauses.append("hypothesis_id = %s")
        params.append(hypothesis_id)
    if status is not None:
        clauses.append("status = %s::integrity_status")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with db.tx() as cur:
        cur.execute(
            "SELECT audit_id, hypothesis_id, status, checklist_results, "
            "missing_protocol_fields, blocking_reasons, audited_at "
            f"FROM integrity_audits {where} "
            "ORDER BY audited_at DESC, audit_id DESC LIMIT %s",
            params,
        )
        return [_row_to_result(dict(r)) for r in cur.fetchall()]


def gate_ready(hypothesis_id: str) -> bool:
    """Whether this hypothesis may cross into Layer 2.

    Takes the LATEST audit across ALL statuses and asks only whether that one
    is a pass.  It does not look for the newest passing audit: that variant
    lets a later failure be overridden by an earlier pass, which is failing
    open exactly when the gate matters most -- a hypothesis that was clean when
    first audited and defective on re-audit would stay enqueueable, and the
    re-audit that was supposed to withdraw it would instead be ignored.

    No audit at all is False. An unchecked hypothesis has not earned the right
    to consume a backtest.
    """
    with db.tx() as cur:
        cur.execute(
            """
            SELECT status
            FROM integrity_audits
            WHERE hypothesis_id = %s
            ORDER BY audited_at DESC, audit_id DESC
            LIMIT 1
            """,
            (hypothesis_id,),
        )
        row = cur.fetchone()
    return row is not None and row["status"] == "pass"


# ---------------------------------------------------------------------
# Static spec gate
# ---------------------------------------------------------------------

#: Words that attach a hedge to a decision. From the checklist's hedge
#: lexicon: each one admits a range, and a range is a hidden parameter.
_HEDGES = (
    "typically",
    "usually",
    "generally",
    "significantly",
    "substantially",
    "materially",
    "in most cases",
    "roughly",
    "approximately",
)

#: From the checklist's discretion lexicon. A judgement call is unlogged
#: parameter fitting: one discretionary step is one extra degree of freedom,
#: and a backtest that tunes it is a backtest that searched.
_DISCRETION = (
    "avoid",
    "if conditions suggest",
    "take profit when",
    "stop when appropriate",
    "hold until",
    "adjust for",
    "manually",
    "as needed",
    "filtered",
    "at our discretion",
)

#: Placeholder features are not features. Layer 1 does not own a variable
#: registry, so this is a lexicon rather than a lookup; the exhaustive
#: registry check is `domains.ALL_VARIABLES` plus a written justification,
#: and the critic owns that one.
_NON_COMPUTABLE_FEATURES = frozenset(
    {"", "-", "?", "n/a", "na", "none", "null", "unknown", "unspecified", "tbd", "todo", "xxx"}
)

#: A condition is a computation when it compares something to something. A
#: sentence with no relational operator is a description of a rule, not a rule,
#: and its hash would be a hash of an accident.
_RELATIONAL = re.compile(r"(<=|>=|==|!=|<|>|=)|\b(?:and|or|not)\b")


def _walk_keys(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Every (dotted-path, key) pair in a nested dict/list, at any depth.

    Recurses through arrays as well as objects, because a forbidden key hidden
    inside a list is the obvious way round a key check that only walks dicts.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            found.append((here, str(key)))
            found.extend(_walk_keys(value, here))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found.extend(_walk_keys(value, f"{path}[{index}]"))
    return found


def lint_spec(yaml_spec: dict[str, Any]) -> list[str]:
    """Static checks on a formalized RuleSpec, decidable without any data.

    The pre-LLM half of the integrity gate.  Everything here is a property of
    the document's shape, so it costs nothing to evaluate, it needs no model,
    and it can be run before a model has had a chance to author the spec -- which
    is the point: a model that has already written the spec will not ask
    whether the spec is prose.

    Returns one message per finding, prefixed with the checklist item it
    belongs to so the caller can route or suppress them individually.  An empty
    list means the spec is clean; it is NOT a pass verdict, which is `audit`'s
    to give and depends on the protocol, not the document.

    Raises ``IntegrityInputError`` on a non-dict: there is no defensible static
    reading of an arbitrary object, and returning a clean list for one would be
    failing open.
    """
    if not isinstance(yaml_spec, dict):
        raise IntegrityInputError(
            f"rule spec must be a dict to be linted, not {type(yaml_spec).__name__}"
        )

    findings: list[str] = []

    forbidden = get_settings().forbidden_spec_keys
    for path, key in _walk_keys(yaml_spec):
        if key.lower() in {f.lower() for f in forbidden}:
            findings.append(
                f"NO_PROFITABILITY_TARGET: {path} names a profitability target; Layer 1 has "
                "no target to optimise and must not be able to see one"
            )

    features = yaml_spec.get("features")
    if not isinstance(features, list) or not features:
        findings.append(
            "IMPOSSIBLE_TO_SOURCE_VARIABLE: features is empty; there is nothing to compute"
        )
    else:
        for feature in features:
            if not isinstance(feature, str) or feature.strip().lower() in _NON_COMPUTABLE_FEATURES:
                findings.append(
                    f"IMPOSSIBLE_TO_SOURCE_VARIABLE: feature {feature!r} names no computable quantity"
                )

    hypothesis = yaml_spec.get("hypothesis")
    signal = hypothesis.get("signal") if isinstance(hypothesis, dict) else None
    condition = signal.get("condition") if isinstance(signal, dict) else None

    if not isinstance(condition, str) or not condition.strip():
        findings.append("AMBIGUOUS_RULE: no signal condition; the rule is prose or absent")
    else:
        if not _RELATIONAL.search(condition):
            findings.append(
                f"AMBIGUOUS_RULE: signal condition {condition!r} contains no comparison, so it "
                "is a description of a rule rather than one"
            )
        lowered = condition.lower()
        for hedge in _HEDGES:
            if hedge in lowered:
                findings.append(
                    f"AMBIGUOUS_RULE: signal condition uses the hedge {hedge!r} on a decision"
                )
                break
        for discretion in _DISCRETION:
            if discretion in lowered:
                findings.append(
                    f"HIDDEN_DISCRETIONARY_DECISION: signal condition contains the discretionary "
                    f"phrase {discretion!r}; a judgement call is unlogged parameter fitting"
                )
                break

    null = hypothesis.get("null") if isinstance(hypothesis, dict) else None
    if not isinstance(null, str) or null.strip().lower() in _NON_COMPUTABLE_FEATURES:
        findings.append(
            f"UNFALSIFIABLE: hypothesis.null is {null!r}; a claim with no refuting observation "
            "cannot come out wrong"
        )

    return findings