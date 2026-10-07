"""Shared Pydantic contracts for Layer 1.

These are the cross-component interface.  Every agent, the API layer and the
pipeline validate against these types, so a change here is a change to the
whole system's contract.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------
# Source collection
# ---------------------------------------------------------------------

SourceType = Literal["youtube", "pdf", "github", "article", "note", "manual"]
Tier = Annotated[int, Field(ge=1, le=5)]


class SourceIn(BaseModel):
    """A research input, before it is assigned an identity."""

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    title: str = Field(min_length=1, max_length=500)
    author: str | None = None
    url: str | None = None
    content: str = Field(min_length=1)
    date: datetime | None = None
    tier: Tier | None = None

    @field_validator("date")
    @classmethod
    def _naive_to_utc(cls, v: datetime | None) -> datetime | None:
        # Naive timestamps are rejected downstream; normalize on the way in
        # so the stored value is always unambiguous.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class Source(BaseModel):
    """An immutable stored source."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_type: SourceType
    title: str
    author: str | None = None
    url: str | None = None
    content: str
    content_sha256: str
    date: datetime
    tier: int
    ingested_at: datetime

    @property
    def word_count(self) -> int:
        return len(self.content.split())


# ---------------------------------------------------------------------
# Mechanism extraction
# ---------------------------------------------------------------------


class Mechanism(BaseModel):
    """An economic mechanism, not a trade signal."""

    model_config = ConfigDict(extra="forbid")

    mechanism_name: str = Field(min_length=1, max_length=200)
    observation: str = Field(min_length=1)
    mechanism_description: str = Field(min_length=1)
    candidate_variables: list[str] = Field(min_length=1)
    domain: str | None = None


class MechanismRecord(Mechanism):
    mechanism_id: str
    source_id: str
    extraction_model: str
    prompt_version: str
    created_at: datetime


# ---------------------------------------------------------------------
# Hypotheses
# ---------------------------------------------------------------------


class Hypothesis(BaseModel):
    """A falsifiable claim.  'Falsifiable' is enforced, not aspirational."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    mechanism_id: str
    statement: str = Field(min_length=1)
    expected_mechanism: str = Field(min_length=1)
    candidate_variables: list[str] = Field(default_factory=list)
    status: str = "proposed"
    generator_model: str
    prompt_version: str
    created_at: datetime


# ---------------------------------------------------------------------
# Formalized rule spec
# ---------------------------------------------------------------------


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=1)
    horizon: str = "unspecified"
    notes: str | None = None


class HypothesisBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str
    signal: Signal
    null: str = Field(
        min_length=1,
        description="The observation that would refute this hypothesis.",
    )


class MarketBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_class: str = "crypto"
    universe: str = "unspecified"


class RuleSpec(BaseModel):
    """The machine-readable YAML contract handed to Layer 2.

    Layer 1 has no notion of a good return.  `model_config.extra='forbid'`
    plus the validator below make an expected_return field unrepresentable.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    name: str
    source: str
    market: MarketBlock
    features: list[str] = Field(min_length=1)
    hypothesis: HypothesisBlock
    expected_mechanism: str
    status: str = "proposed"

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in v).strip("_")
        if not cleaned:
            raise ValueError("name must contain at least one alphanumeric character")
        return cleaned

    def spec_sha256(self) -> str:
        """Content hash used for versioning and research_id derivation."""
        canonical = self.model_dump(mode="json", exclude={"status"})
        import json

        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(
            self.model_dump(mode="json"), sort_keys=False, width=100
        )


class RuleSpecRecord(RuleSpec):
    version_id: int
    model_used: str
    prompt_version: str
    creation_timestamp: datetime


# ---------------------------------------------------------------------
# Critic
# ---------------------------------------------------------------------

CritiqueDimension = Literal[
    "mechanism_plausible",
    "measurable",
    "falsifiable",
    "distinct",
    "no_hidden_discretion",
    "no_future_leakage",
    "no_prior_failure_collision",
]

VERDICT = Literal["PASS", "FAIL"]
FailureType = Literal[
    "implausible_mechanism",
    "not_measurable",
    "not_falsifiable",
    "not_distinct",
    "hidden_discretion",
    "future_information_leak",
    "prior_failure_precedent",
    "unformalizable",
    "malformed_llm_output",
]


class Critique(BaseModel):
    """The Research Critic's hostile verdict.

    A FAIL must name the dimension and a failure_type.  The critic cannot
    express a vague rejection.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    verdict: VERDICT
    scores: dict[str, int] = Field(
        description="Per-dimension score 0-2: 0=fail, 1=weak, 2=sound."
    )
    failure_type: FailureType | None = None
    reason: str = Field(min_length=1)
    suggestions: list[str] = Field(default_factory=list)
    critic_model: str
    prompt_version: str

    @model_validator(mode="after")
    def _coherent(self) -> "Critique":
        if self.verdict == "FAIL" and not self.failure_type:
            raise ValueError("FAIL verdict requires a failure_type")
        if self.verdict == "PASS" and self.failure_type:
            raise ValueError("PASS verdict must not carry a failure_type")
        if any(v not in (0, 1, 2) for v in self.scores.values()):
            raise ValueError("scores must be 0, 1 or 2")
        return self


# ---------------------------------------------------------------------
# Queue / provenance
# ---------------------------------------------------------------------


class ExperimentJob(BaseModel):
    """A row in the Layer 2 queue, as returned by GET /experiments/queue."""

    model_config = ConfigDict(extra="allow")

    experiment_id: str
    research_id: str
    hypothesis_id: str
    version_id: int
    feature_set: list[str]
    rule_version: str
    data_version: str
    parameter_space: dict[str, Any] = Field(default_factory=dict)
    status: str
    queued_at: datetime
    # Provenance, joined in for Layer 2 so it needs no second call.
    hypothesis_statement: str | None = None
    expected_mechanism: str | None = None
    rule_spec: dict[str, Any] | None = None
    source_ids: list[str] = Field(default_factory=list)
    model_used: str | None = None
    prompt_version: str | None = None
    creation_timestamp: datetime | None = None


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    failure_id: int
    hypothesis_id: str
    reason: str
    failure_type: str
    stage: str
    reported_by: str
    created_at: datetime