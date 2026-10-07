"""Pydantic contracts for the Evidence Ledger.

Separate from :mod:`layer1.contracts` because these objects answer a
different question.  ``contracts.py`` describes artefacts that already
carry an identity handed out by a sequence (``SRC_001``, ``H012``); the
ledger's central object -- a claim -- has no sequence in the schema, so its
identity is minted in Python.  Keeping the two apart also keeps the
sequence-based ID contract stable while the ledger evolves.

The four claim types are the intellectual core of this file.  They are the
storage form of the FACT / CLAIM / HYPOTHESIS / RESULT distinction in
``knowledge/research_integrity/knowledge_leakage.md``:

  * ``fact``             -- a state of the world, checkable against data.
  * ``author_claim``     -- an assertion by a source about a relationship.
  * ``empirical_result`` -- a measurement the source reports having made.
  * ``model_inference``  -- OUR model's reasoning.  Never literature.

The silent slide this module exists to prevent is ``model_inference ->
empirical_result``: a model asserts a finding it never read, the sentence
is grammatical, and it is filed next to things that were measured.  That is
the KTD-Fin failure mode -- memorisation wearing the costume of analysis --
and once it is stored, nothing downstream can detect it.  Two structural
guards live here: ``claim_type`` has NO default in every input model, so a
caller must state the epistemic type explicitly, and ``ClaimIn`` forbids
extra fields so a caller cannot smuggle a parallel "this was measured"
marker alongside a ``model_inference`` type.

Nothing in this module, and nothing in the ledger, carries a return,
Sharpe or profitability field.  Quality of evidence is not value of a
trade, and Layer 1 has no target to optimise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ClaimType",
    "ConflictReason",
    "ResolutionStatus",
    "FutureInformationRisk",
    "MODEL_PRODUCED_CLAIM_TYPE",
    "SOURCE_EVIDENCE_CLAIM_TYPES",
    "CLAIM_TYPE_GUIDANCE",
    "ClaimIn",
    "Claim",
    "ClaimContradictionIn",
    "ClaimContradiction",
    "SourceTemporalContextIn",
    "SourceTemporalContext",
]

# ---------------------------------------------------------------------
# Enumerations -- these mirror Postgres enums exactly.
# ---------------------------------------------------------------------

ClaimType = Literal["fact", "author_claim", "empirical_result", "model_inference"]

ConflictReason = Literal[
    "market",
    "timeframe",
    "cost_model",
    "feature_definition",
    "sampling_frequency",
    "regime",
]

ResolutionStatus = Literal[
    "unresolved",
    "hypothesis_generated",
    "explained",
    "dismissed",
]

FutureInformationRisk = Literal["none", "low", "medium", "high", "unknown"]

#: The only type a claim produced by our own model may carry.
#:
#: Named as a constant rather than only documented, so a caller writing a
#: model-output path has something to import and cannot drift onto a
#: literal.
MODEL_PRODUCED_CLAIM_TYPE: ClaimType = "model_inference"

#: The types that may legitimately be attributed to a source rather than to
#: us.  Anything NOT in this set came from our own reasoning.
SOURCE_EVIDENCE_CLAIM_TYPES: frozenset[str] = frozenset(
    {"fact", "author_claim", "empirical_result"}
)

#: What each type may be used to support.  Kept as data rather than prose so
#: a caller can print it, an API can serve it, and a reviewer can check the
#: table against the source doc without reading this file.
CLAIM_TYPE_GUIDANCE: dict[str, str] = {
    "fact": (
        "A state of the world at a time. Checkable against data. Supports "
        "nothing predictive on its own."
    ),
    "author_claim": (
        "An assertion by a source about a relationship. Supports a "
        "hypothesis to be tested; is not evidence of anything."
    ),
    "empirical_result": (
        "A measurement the source reports having performed. Evidence on "
        "that source's data, at that version. Never transferable to ours."
    ),
    "model_inference": (
        "Produced by OUR model, not read from the source. May motivate a "
        "hypothesis; may never be cited as literature or as a result."
    ),
}

#: Point-in-time keys accepted inside ``time_context``.  JSONB has no date
#: type, so values are normalised to ISO-8601 UTC strings on the way in --
#: otherwise "2019-01-01" and "2019-01-01T00:00:00Z" are the same instant
#: written two ways and a look-ahead comparison silently misses.
_TIME_CONTEXT_KEYS = ("data_start", "data_end", "published_at")


def _to_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp rather than guessing its zone."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _normalize_instant(value: Any) -> Any:
    """Normalise one JSONB point-in-time value to an ISO-8601 UTC string.

    Returns the input unchanged when it is not a parseable timestamp; this
    is provenance metadata, not a validated field, and refusing a source
    because its date string was unusual would lose the claim entirely.
    """
    if isinstance(value, str):
        try:
            return _to_utc(datetime.fromisoformat(value)).isoformat()
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------
# Claims -- typed evidence
# ---------------------------------------------------------------------


class ClaimIn(BaseModel):
    """A claim before it is assigned an identity.

    ``claim_type`` has no default.  A caller that forgets it gets a
    ``TypeError`` at the call site, which is the moment the epistemic type
    should be decided -- not a stored row typed by whatever happened to be
    nearby in the code.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_type: ClaimType
    section_ref: str | None = None
    evidence_strength: str | None = None
    time_context: dict[str, Any] | None = None
    agent_interpretation: str | None = None

    @field_validator("statement")
    @classmethod
    def _statement_not_blank(cls, v: str) -> str:
        # An empty claim is worse than no claim: it is a row that later
        # generations can cite as though something had been extracted.
        if not v.strip():
            raise ValueError(
                "statement must contain text; an empty claim is not evidence "
                "and must not be stored"
            )
        return v

    @field_validator("time_context")
    @classmethod
    def _normalize_time_context(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return None
        return {k: (_normalize_instant(val) if k in _TIME_CONTEXT_KEYS else val) for k, val in v.items()}


class Claim(ClaimIn):
    """A stored claim, as read back from the append-only ``claims`` table.

    There is no UPDATE path and no ``updated_at``: correcting a reading of
    the source means writing a new claim, so the record of what we believed
    when a downstream hypothesis was generated survives.
    """

    claim_id: str
    created_at: datetime


# ---------------------------------------------------------------------
# Contradictions -- conflict is research output
# ---------------------------------------------------------------------


class ClaimContradictionIn(BaseModel):
    """A recorded disagreement between two claims, before identity."""

    model_config = ConfigDict(extra="forbid")

    claim_a: str = Field(min_length=1)
    claim_b: str = Field(min_length=1)
    #: An array because several reasons can hold at once, and none has to
    #: be verified for the conflict to be worth recording.  Non-empty by
    #: construction: ``knowledge/methods/contradiction_resolution.md`` is
    #: explicit that a conflict record which says "they used different
    #: assumptions" has recorded nothing.  Prose is not a reason.
    possible_reasons: Annotated[list[ConflictReason], Field(min_length=1)]
    spawned_hypothesis_id: str | None = None
    resolution_status: ResolutionStatus = "unresolved"

    @field_validator("possible_reasons")
    @classmethod
    def _dedupe_reasons(cls, v: list[str]) -> list[str]:
        # Order is preserved (it is the caller's checklist order); only
        # repeats are dropped, so the stored array is a set that reads like
        # the enumeration.
        seen: set[str] = set()
        out: list[str] = []
        for reason in v:
            if reason not in seen:
                seen.add(reason)
                out.append(reason)
        return out

    @model_validator(mode="after")
    def _distinct_claims(self) -> "ClaimContradictionIn":
        if self.claim_a == self.claim_b:
            raise ValueError(
                f"claim {self.claim_a!r} cannot contradict itself; that is a "
                "data error (a duplicated or mis-extracted claim), not a "
                "cross-source conflict, and analysing it would be analysing "
                "our own bug"
            )
        return self

    @model_validator(mode="after")
    def _hypothesis_lifecycle(self) -> "ClaimContradictionIn":
        # The schema comment states the invariant; enforcing it here means
        # the failure is a message rather than an FK or CHECK error, and a
        # conflict cannot claim it spawned a hypothesis when it did not.
        if self.resolution_status == "hypothesis_generated" and not self.spawned_hypothesis_id:
            raise ValueError(
                "resolution_status='hypothesis_generated' requires "
                "spawned_hypothesis_id; a conflict cannot be resolved into a "
                "hypothesis that was never recorded"
            )
        if self.resolution_status != "hypothesis_generated" and self.spawned_hypothesis_id:
            raise ValueError(
                f"spawned_hypothesis_id={self.spawned_hypothesis_id!r} is only "
                f"permitted with resolution_status='hypothesis_generated', not "
                f"{self.resolution_status!r}"
            )
        return self


class ClaimContradiction(ClaimContradictionIn):
    """A stored conflict row."""

    contradiction_id: str
    created_at: datetime


# ---------------------------------------------------------------------
# Source temporal context -- point-in-time provenance
# ---------------------------------------------------------------------


class SourceTemporalContextIn(BaseModel):
    """When a source's content could first have been known.

    The rule being encoded: a source available at time T may not use
    information that only became available after T.  That check has to run
    against ``information_available_at``, not ``publication_date``, because
    a December backtest read in February already knows things a December
    trader did not.  Recording both, and refusing the incoherent ordering,
    is what stops look-ahead arriving through metadata rather than through
    data.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    #: Declared publication date.  Null when the source states none --
    #: absence is recorded rather than guessed.
    publication_date: datetime | None = None
    #: The earliest time the content could have informed a decision.  NOT
    #: NULL in the schema, and not optional here: we must be able to record
    #: that we do not know a date, which is different from not recording it.
    information_available_at: datetime
    #: Defaults to 'unknown' because unknown is the honest state, and a risk
    #: never assessed must not read as absent.
    future_information_risk: FutureInformationRisk = "unknown"

    @field_validator("publication_date", "information_available_at")
    @classmethod
    def _naive_to_utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _to_utc(v)

    @model_validator(mode="after")
    def _point_in_time_coherent(self) -> "SourceTemporalContextIn":
        pub = self.publication_date
        avail = self.information_available_at
        if pub is not None and pub > avail:
            raise ValueError(
                f"publication_date {pub.isoformat()} is AFTER "
                f"information_available_at {avail.isoformat()}: content cannot "
                "have been knowable before it was published. Either the dates "
                "are transposed or 'information_available_at' has been "
                "backdated to admit look-ahead -- fix the provenance rather "
                "than the check"
            )
        return self


class SourceTemporalContext(SourceTemporalContextIn):
    """A stored temporal-provenance row."""

    model_config = ConfigDict(extra="forbid")
