"""RESEARCH QUALITY scoring, and the evolutionary lineage of the corpus.

**This module scores RESEARCH QUALITY.  It does not score trading
performance, it is not a proxy for trading performance, and it must never be
used as one.**  A hypothesis can score 40/40 here and still lose money.
Layer 2 decides profitability, with data, once and only once; Layer 1 holds no
target of any kind, and nothing in this file reads, stores, or optimises
toward one.  A research layer that can see a profitability target stops being
a research layer and becomes a machine for generating hypotheses that flatter
a backtest, which is a different and much cheaper activity than finding out
what is true.  Read `knowledge/methods/hypothesis_testing.md` §"What this score
is not" before adding a dimension.

Two instruments live here, and they are unrelated on purpose:

* `QualityScore` -- eight dimensions, 0-5 each, 0-40 total, banded.  It
  answers one question: *is this claim well-specified and well-motivated
  enough to deserve a Layer 2 experiment slot?*
* `Generation` -- the recorded parent/child edges that make the corpus a tree
  rather than a flat pile of unrelated strings.  It answers a different
  question: *how many times has this research family been revised, and did the
  revisions converge or diverge?*

Neither instrument is a performance measurement, and neither may be joined to
one and treated as an optimisation target.

DIRECTION WARNING -- `confounding_risk` is a trap
---------------------------------------------------
Seven of the eight dimensions share one direction: higher is better.  The
eighth, `confounding_risk`, is stored under a name that reads the other way --
a *high confounding risk* would normally be bad -- but it is scored so that
higher is better, because the dimension is really "how well is the effect
separated from its alternatives", and 5 means the design does separate them.
All eight therefore add into one comparable number.  The database cannot catch
a sign error here: it checks that the total equals the sum, not what the parts
mean.  If you are ever tempted to invert this dimension, read this paragraph
again first.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import db
from .domains import ALL_VARIABLES

__all__ = [
    "QUALITY_DIMENSIONS",
    "PRIORITY_BANDS",
    "MUTATION_TYPES",
    "PROVISIONAL_RATIONALE",
    "QualityError",
    "DimensionError",
    "MutationTypeError",
    "QualityScore",
    "Generation",
    "band_for",
    "score_hypothesis",
    "get_score",
    "list_scores",
    "rank_queue",
    "record_generation",
    "list_generations",
    "lineage",
    "family_of",
    "refine_count",
]


# ---------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------

#: The eight dimensions, in schema order.  The order is load-bearing: the
#: INSERT column list and the VALUES list are both generated from it, so a
#: reordering is safe but a reordering that changed `len()` would not be.
QUALITY_DIMENSIONS: tuple[str, ...] = (
    "mechanism_clarity",
    "falsifiability",
    "evidence_quality",
    "novelty",
    "data_availability",
    "temporal_integrity",
    "implementation_clarity",
    "confounding_risk",  # higher = LOWER risk. See the module docstring.
)

#: (inclusive lower bound, band) pairs, ascending.  Bands are a statement
#: about one hypothesis; the raw total is a comparison between two.  They are
#: deliberately not one number: a hypothesis in the `experiment` band deserves
#: a slot even when a much weaker idea happens to outrank it, and a rank has
#: no way to say that.
PRIORITY_BANDS: tuple[tuple[int, str], ...] = (
    (0, "reject"),
    (21, "weak"),
    (31, "experiment"),
    (36, "high_priority"),
)

#: The seven mutations, matching the `mutation_type` enum exactly.  Validated
#: here rather than left to the enum so the error names the valid values; an
#: untyped revision is the flat-pile failure mode this whole table exists to
#: prevent, so it is refused at the boundary rather than at the database.
MUTATION_TYPES: tuple[str, ...] = (
    "new",
    "refinement",
    "generalization",
    "specialization",
    "combination",
    "contradiction",
    "replication",
)

MutationType = Literal[
    "new",
    "refinement",
    "generalization",
    "specialization",
    "combination",
    "contradiction",
    "replication",
]

MAX_DIMENSION = 5
MAX_TOTAL = MAX_DIMENSION * len(QUALITY_DIMENSIONS)

#: Stamped on any score computed without an explicit judgement, so that a row
#: written by the heuristic can never be mistaken later for one a reviewer
#: stood behind.
PROVISIONAL_RATIONALE = (
    "provisional: derived from stored state by a deterministic heuristic, "
    "not judged. Pass explicit dimension scores when they are available."
)

#: Strings that mean "this field was filled in, but nothing is in it".
_PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "?",
        "n/a",
        "na",
        "none",
        "null",
        "tba",
        "tbd",
        "todo",
        "to be determined",
        "unknown",
        "unspecified",
    }
)

#: A falsification condition has to be timed.  "there is no relationship" is
#: unfalsifiable in practice because there is no horizon on which its absence
#: could have been observed; a horizon is what makes the observation concrete.
_HORIZON_RE = re.compile(
    r"\d+\s*(?:ms|s|m|h|d|w|sec|secs|second|seconds|min|mins|minute|minutes"
    r"|hour|hours|day|days|week|weeks|bar|bars|candle|candles|tick|ticks"
    r"|session|sessions)\b"
    r"|\b(?:next|following)\b[^.]{0,24}\b(?:bar|candle|close|open|session|tick"
    r"|minute|hour|day)\b",
    re.IGNORECASE,
)

#: Cue phrases that a hypothesis actually considered what else could have
#: produced the observation.  Deliberately shallow: this is a proxy signal
#: that the confound was *considered*, not evidence that it was *excluded*.
_CONFOUND_CUES: tuple[str, ...] = (
    "alternative",
    "confound",
    "rather than",
    "instead of",
    "distinguish",
    "separate",
    "control for",
    "controlled for",
    "difference-in-difference",
    "placebo",
    "versus",
)


class QualityError(ValueError):
    """A scoring or lineage request that must not be honoured as written."""


class DimensionError(QualityError):
    """A dimension name or value that cannot be scored."""


class MutationTypeError(QualityError):
    """A revision that cannot be typed."""


# ---------------------------------------------------------------------
# models
# ---------------------------------------------------------------------


class QualityScore(BaseModel):
    """One hypothesis's research-quality record, exactly as stored.

    `total_score` is derived, never supplied: the database constrains it to
    equal the sum of the eight, and the model re-checks the same identity plus
    the band, so a score that is internally inconsistent is unrepresentable in
    Python as well as in SQL.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    mechanism_clarity: int = Field(ge=0, le=MAX_DIMENSION)
    falsifiability: int = Field(ge=0, le=MAX_DIMENSION)
    evidence_quality: int = Field(ge=0, le=MAX_DIMENSION)
    novelty: int = Field(ge=0, le=MAX_DIMENSION)
    data_availability: int = Field(ge=0, le=MAX_DIMENSION)
    temporal_integrity: int = Field(ge=0, le=MAX_DIMENSION)
    implementation_clarity: int = Field(ge=0, le=MAX_DIMENSION)
    confounding_risk: int = Field(
        ge=0,
        le=MAX_DIMENSION,
        description=(
            "Separation from alternative explanations, not the amount of risk. "
            "Higher is better: 5 means the design separates the effect from "
            "its main competitors."
        ),
    )
    total_score: int
    priority_band: str
    rationale: str | None = None
    scored_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> "QualityScore":
        total = sum(getattr(self, dim) for dim in QUALITY_DIMENSIONS)
        if self.total_score != total:
            raise ValueError(
                f"total_score {self.total_score} != sum of dimensions {total}"
            )
        expected = band_for(total)
        if self.priority_band != expected:
            raise ValueError(
                f"priority_band {self.priority_band!r} does not match total "
                f"{total}; expected {expected!r}"
            )
        return self

    def dimensions(self) -> dict[str, int]:
        """The eight values keyed by name, for display and for diffing."""
        return {dim: getattr(self, dim) for dim in QUALITY_DIMENSIONS}


class Generation(BaseModel):
    """One recorded step of a hypothesis's evolutionary history."""

    model_config = ConfigDict(extra="forbid")

    generation_id: str
    hypothesis_id: str = Field(
        description="The hypothesis this generation produced."
    )
    parent_hypothesis_id: str | None = Field(
        default=None, description="Null only for a lineage root."
    )
    generation_number: int = Field(
        ge=1, description="Depth in the tree; a root is 1."
    )
    mutation_type: MutationType
    revision_reason: str = Field(min_length=1)
    critique_summary: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------

_SCORE_VALUE_COLUMNS = ", ".join(QUALITY_DIMENSIONS)
_SCORE_RETURNING = (
    f"hypothesis_id, {_SCORE_VALUE_COLUMNS}, total_score, priority_band, "
    "rationale, scored_at"
)

_GENERATION_COLUMNS = (
    "generation_id, hypothesis_id, parent_hypothesis_id, generation_number, "
    "mutation_type, revision_reason, critique_summary, created_at"
)

_KNOWN_VARIABLES = frozenset(ALL_VARIABLES)


def band_for(total: int) -> str:
    """The band a total falls in.  Boundaries are inclusive at the bottom."""
    if not isinstance(total, int) or isinstance(total, bool):
        raise DimensionError(f"total must be an int, got {type(total).__name__}")
    if not 0 <= total <= MAX_TOTAL:
        raise DimensionError(f"total {total} outside 0..{MAX_TOTAL}")
    band = PRIORITY_BANDS[0][1]
    for lower, name in PRIORITY_BANDS:
        if total >= lower:
            band = name
    return band


def _validate_scores(scores: dict[str, int]) -> dict[str, int]:
    """Normalise a partial score block to all eight dimensions.

    Unknown names are rejected rather than dropped: a typo'd dimension name
    would otherwise silently score 0 and quietly drag a hypothesis down a band.
    Omitted dimensions default to 0, which is the fail-closed reading shared
    with the critic -- a dimension nobody stated has not been established as
    sound.
    """
    if not isinstance(scores, dict):
        raise DimensionError(f"scores must be a mapping, got {type(scores).__name__}")
    unknown = sorted(set(scores) - set(QUALITY_DIMENSIONS))
    if unknown:
        raise DimensionError(
            f"unknown quality dimension(s) {unknown}; "
            f"valid dimensions are {list(QUALITY_DIMENSIONS)}"
        )
    normalised: dict[str, int] = {}
    for dim in QUALITY_DIMENSIONS:
        value = scores.get(dim, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise DimensionError(
                f"dimension {dim!r} must be an int, got "
                f"{type(value).__name__}: {value!r}"
            )
        if not 0 <= value <= MAX_DIMENSION:
            raise DimensionError(
                f"dimension {dim!r} must be 0..{MAX_DIMENSION}, got {value}"
            )
        normalised[dim] = value
    return normalised


def _hypothesis_exists(cur, hypothesis_id: str) -> bool:
    cur.execute(
        "SELECT 1 AS present FROM hypotheses WHERE hypothesis_id = %s",
        (hypothesis_id,),
    )
    return cur.fetchone() is not None


def _require_hypothesis(cur, hypothesis_id: str) -> None:
    if not _hypothesis_exists(cur, hypothesis_id):
        # The FK would catch this too, but as a bare ForeignKeyViolation
        # naming a table. This names the thing the caller got wrong.
        raise QualityError(f"unknown hypothesis {hypothesis_id!r}")


# -- the deterministic proxy ------------------------------------------------
#
# `score_hypothesis(scores=None)` derives the eight dimensions from stored
# state.  It is a PROXY, not a judgement: it can only see what has been
# recorded, and most of what it reads is a proxy for what has been recorded.
# Its purpose is that a corpus with no reviewer must still be rankable, and a
# fail-open default (everyone scores 20) would make the band meaningless.  It
# is deterministic on purpose: a heuristic whose output depends on a language
# model is a model wearing a deterministic costume, and it would be
# unreproducible, unrankable and impossible to test.  Callers that have real
# judgements must pass `scores`.


def _mechanism_clarity(expected_mechanism: str) -> tuple[int, str]:
    text = (expected_mechanism or "").strip()
    if text.lower().strip(".") in _PLACEHOLDERS:
        return 0, "expected_mechanism is a placeholder, so nothing is explained"
    words = len(text.split())
    if words < 4:
        return 1, f"expected_mechanism is {words} words; too short to be a cause"
    if words < 12:
        return 3, "expected_mechanism states a mechanism in one clause"
    return 5, "expected_mechanism gives a substantive causal account"


def _falsifiability(null_statement: str | None) -> tuple[int, str]:
    if null_statement is None:
        return 0, "no formalized spec, so no falsification condition exists"
    text = null_statement.strip()
    if text.lower().strip(".") in _PLACEHOLDERS:
        return 0, "the spec's null is a placeholder"
    if not _HORIZON_RE.search(text):
        return 2, "the null states an outcome but names no horizon"
    return 5, "the null names both the observation and the horizon"


def _evidence_quality(tier: str | None, claim_types: dict[str, int]) -> tuple[int, str]:
    """Provenance standing only.  A prestigious source can carry a claim that
    nobody ever checked, and a good journal can be cited badly; this is why the
    evidence dimension is about what was recorded, not about truth."""
    score = 1  # our own generation, with no external provenance behind it
    notes: list[str] = []
    if tier is not None and tier.isdigit() and int(tier) <= 3:
        score += 1
        notes.append(f"source tier {tier}")
    # `model_inference` is deliberately worth nothing here: it is our own
    # output, not literature evidence, and crediting it would let the system
    # grade its own homework.
    grounded = min(
        2,
        (claim_types.get("fact", 0) > 0) + (claim_types.get("empirical_result", 0) > 0),
    )
    cited = claim_types.get("author_claim", 0) > 0
    score += grounded + int(cited)
    if claim_types.get("model_inference", 0):
        notes.append("model_inference claims present; credited to nothing")
    if not notes:
        notes.append("no provenance beyond our own generation")
    return min(MAX_DIMENSION, score), "; ".join(notes)


def _novelty(has_parent: bool, not_distinct: bool, family_revisions: int) -> tuple[int, str]:
    score = 4
    notes: list[str] = []
    if has_parent:
        score -= 1
        notes.append("declared revision of an existing idea")
    if not_distinct:
        score -= 2
        notes.append("a not_distinct failure is already recorded against it")
    if family_revisions >= 3:
        score -= 1
        notes.append(f"family already revised {family_revisions} times")
    if len(notes) == 1:
        notes = ["distinct from everything recorded in the corpus"]
    return max(0, score), "; ".join(notes)


def _data_availability(candidate_variables: list[str]) -> tuple[int, str]:
    if not candidate_variables:
        return 0, "no candidate variables declared"
    known = sum(1 for v in candidate_variables if v in _KNOWN_VARIABLES)
    ratio = known / len(candidate_variables)
    if ratio == 1.0:
        return 5, "every candidate variable resolves in layer1.domains"
    if ratio >= 0.5:
        return 3, f"{known}/{len(candidate_variables)} variables resolve in domains"
    return 1, f"only {known}/{len(candidate_variables)} variables resolve in domains"


#: Point-in-time provenance risk -> a "temporal integrity" score.  `unknown`
#: is scored 1, not 5, and a missing row is treated as unknown: a risk that was
#: never assessed must not read as an absence of risk.
_FUTURE_RISK_POINTS: dict[str, int] = {
    "none": 5,
    "low": 4,
    "medium": 2,
    "high": 0,
    "unknown": 1,
}


def _temporal_integrity(future_risk: str | None) -> tuple[int, str]:
    risk = (future_risk or "unknown").strip().lower()
    points = _FUTURE_RISK_POINTS.get(risk, 1)
    return points, f"source_temporal_context.future_information_risk={risk}"


def _implementation_clarity(
    has_spec: bool, audit_status: str | None, missing_fields: list[str] | None
) -> tuple[int, str]:
    if not has_spec:
        return 0, "no formalized spec exists, so nothing is specified"
    score = 3
    notes = ["a formalized spec exists"]
    if missing_fields is None:
        notes.append("no integrity audit, so completeness is unverified")
    elif missing_fields:
        notes.append(f"audit reports missing protocol fields: {missing_fields}")
    else:
        score += 1
        notes.append("every protocol block is defined")
    if audit_status == "pass":
        score += 1
        notes.append("integrity audit passed")
    return min(MAX_DIMENSION, score), "; ".join(notes)


def _confounding_separation(
    statement: str, mechanism: str, variables: list[str], has_spec: bool
) -> tuple[int, str]:
    """Score how separated the effect is from its alternatives -- higher is
    BETTER, despite the column name.  See the module docstring."""
    text = f"{statement} {mechanism}".lower()
    score = 2
    notes = ["no alternative explanation recorded"]
    if any(cue in text for cue in _CONFOUND_CUES):
        score += 2
        notes = ["an alternative explanation is named in the claim"]
    if len(variables) >= 2:
        score += 1
    if not has_spec:
        # Nothing concrete means nothing that could be separated: before the
        # claim is specified, "separated from alternatives" is not a property
        # it can have yet.
        score = min(score, 1)
        notes.append("nothing specified, so nothing can be separated")
    return min(MAX_DIMENSION, score), "; ".join(notes)


def _provisional_state(hypothesis_id: str) -> dict[str, Any]:
    """Everything the proxy reads, in one transaction so it sees one snapshot."""
    with db.tx() as cur:
        cur.execute(
            """
            SELECT h.statement, h.expected_mechanism, h.candidate_variables,
                   rn.source_id, s.tier
            FROM hypotheses h
            JOIN research_notes rn ON rn.mechanism_id = h.mechanism_id
            LEFT JOIN sources s ON s.source_id = rn.source_id
            WHERE h.hypothesis_id = %s
            """,
            (hypothesis_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise QualityError(f"unknown hypothesis {hypothesis_id!r}")

        cur.execute(
            "SELECT claim_type, count(*) AS n FROM claims "
            "WHERE source_id = %s GROUP BY claim_type",
            (row["source_id"],),
        )
        claim_types = {r["claim_type"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            "SELECT count(*) AS n FROM hypothesis_versions WHERE hypothesis_id = %s",
            (hypothesis_id,),
        )
        has_spec = int(cur.fetchone()["n"]) > 0

        cur.execute(
            "SELECT yaml_spec FROM hypothesis_versions "
            "WHERE hypothesis_id = %s ORDER BY version_id DESC LIMIT 1",
            (hypothesis_id,),
        )
        spec_row = cur.fetchone()
        null_statement: str | None = None
        if spec_row is not None:
            hypothesis_block = (spec_row["yaml_spec"] or {}).get("hypothesis") or {}
            raw = hypothesis_block.get("null")
            null_statement = raw if isinstance(raw, str) else None

        cur.execute(
            "SELECT future_information_risk FROM source_temporal_context "
            "WHERE source_id = %s",
            (row["source_id"],),
        )
        temporal = cur.fetchone()
        future_risk = temporal["future_information_risk"] if temporal else None

        cur.execute(
            """
            SELECT status, missing_protocol_fields
            FROM integrity_audits
            WHERE hypothesis_id = %s
            ORDER BY audited_at DESC, audit_id DESC
            LIMIT 1
            """,
            (hypothesis_id,),
        )
        audit = cur.fetchone()

        cur.execute(
            "SELECT count(*) AS n FROM failures "
            "WHERE hypothesis_id = %s AND failure_type = 'not_distinct'",
            (hypothesis_id,),
        )
        not_distinct = int(cur.fetchone()["n"]) > 0

        cur.execute(
            "SELECT count(*) AS n FROM research_generations "
            "WHERE hypothesis_id = %s AND parent_hypothesis_id IS NOT NULL",
            (hypothesis_id,),
        )
        has_parent = int(cur.fetchone()["n"]) > 0

    return {
        "statement": row["statement"],
        "expected_mechanism": row["expected_mechanism"],
        "candidate_variables": list(row["candidate_variables"] or []),
        "tier": row["tier"],
        "claim_types": claim_types,
        "has_spec": has_spec,
        "null_statement": null_statement,
        "future_risk": future_risk,
        "audit_status": audit["status"] if audit else None,
        "missing_fields": list(audit["missing_protocol_fields"] or [])
        if audit
        else None,
        "not_distinct": not_distinct,
        "has_parent": has_parent,
    }


def _provisional_scores(hypothesis_id: str) -> tuple[dict[str, int], str]:
    """Derive the eight dimensions from stored state, with the reasons."""
    state = _provisional_state(hypothesis_id)
    variables: list[str] = state["candidate_variables"]

    # Novelty is the one dimension that depends on the corpus rather than on
    # this row, so it needs the family; the rest are local reads.
    family_revisions = refine_count(hypothesis_id)

    values: dict[str, int] = {}
    reasons: dict[str, str] = {}

    def put(dim: str, pair: tuple[int, str]) -> None:
        values[dim], reasons[dim] = pair

    put("mechanism_clarity", _mechanism_clarity(state["expected_mechanism"]))
    put("falsifiability", _falsifiability(state["null_statement"]))
    put("evidence_quality", _evidence_quality(state["tier"], state["claim_types"]))
    put(
        "novelty",
        _novelty(state["has_parent"], state["not_distinct"], family_revisions),
    )
    put("data_availability", _data_availability(variables))
    put("temporal_integrity", _temporal_integrity(state["future_risk"]))
    put(
        "implementation_clarity",
        _implementation_clarity(
            state["has_spec"], state["audit_status"], state["missing_fields"]
        ),
    )
    put(
        "confounding_risk",
        _confounding_separation(
            state["statement"], state["expected_mechanism"], variables, state["has_spec"]
        ),
    )

    rationale = PROVISIONAL_RATIONALE + " Reasons: " + "; ".join(
        f"{dim}={values[dim]} ({reasons[dim]})" for dim in QUALITY_DIMENSIONS
    )
    return {dim: values[dim] for dim in QUALITY_DIMENSIONS}, rationale


def _score_from_row(row: dict[str, Any]) -> QualityScore:
    return QualityScore.model_validate(row)


# ---------------------------------------------------------------------
# quality scoring
# ---------------------------------------------------------------------


def score_hypothesis(
    hypothesis_id: str,
    *,
    scores: dict[str, int] | None = None,
    rationale: str | None = None,
) -> QualityScore:
    """Score one hypothesis's RESEARCH QUALITY and persist it.

    `scores` is the eight dimensions on 0-5.  When it is omitted the score is
    derived from stored state by a documented deterministic proxy -- a proxy,
    not a judgement, and the returned row says so in its `rationale`.  Nothing
    here consults a model: deterministic ranking belongs in software, because a
    score that changes from run to run cannot be used to rank anything.

    Raises `DimensionError` for an unknown dimension name or a value outside
    0-5, and `QualityError` for a hypothesis that does not exist.
    """
    if scores is None:
        values, derived_rationale = _provisional_scores(hypothesis_id)
        if rationale is None:
            rationale = derived_rationale
    else:
        values = _validate_scores(scores)

    total = sum(values[dim] for dim in QUALITY_DIMENSIONS)
    band = band_for(total)
    if rationale is not None:
        rationale = rationale.strip() or None

    with db.tx() as cur:
        _require_hypothesis(cur, hypothesis_id)
        cur.execute(
            f"""
            INSERT INTO hypothesis_quality AS q (
                hypothesis_id, {_SCORE_VALUE_COLUMNS},
                total_score, priority_band, rationale
            )
            VALUES (
                %s, {", ".join(["%s"] * len(QUALITY_DIMENSIONS))}, %s, %s, %s
            )
            ON CONFLICT (hypothesis_id) DO UPDATE SET
                {", ".join(f"{dim} = EXCLUDED.{dim}" for dim in QUALITY_DIMENSIONS)},
                total_score  = EXCLUDED.total_score,
                priority_band = EXCLUDED.priority_band,
                rationale     = EXCLUDED.rationale,
                scored_at     = now()
            RETURNING {_SCORE_RETURNING}
            """,
            (
                hypothesis_id,
                *(values[dim] for dim in QUALITY_DIMENSIONS),
                total,
                band,
                rationale,
            ),
        )
        row = cur.fetchone()

    return _score_from_row(row)


def get_score(hypothesis_id: str) -> QualityScore | None:
    """The stored score, or None when the hypothesis has never been scored."""
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_SCORE_RETURNING} FROM hypothesis_quality "
            "WHERE hypothesis_id = %s",
            (hypothesis_id,),
        )
        row = cur.fetchone()
    return _score_from_row(row) if row else None


def list_scores(priority_band: str | None = None, limit: int = 100) -> list[QualityScore]:
    """Scored hypotheses, strongest first.

    Ordering is by total with the id as tie-break, so the order is total and
    reproducible rather than dependent on insertion.
    """
    bands = {name for _, name in PRIORITY_BANDS}
    if priority_band is not None and priority_band not in bands:
        raise QualityError(
            f"unknown priority_band {priority_band!r}; expected one of {sorted(bands)}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise QualityError(f"limit must be a positive int, got {limit!r}")

    sql = f"SELECT {_SCORE_RETURNING} FROM hypothesis_quality"
    params: list[Any] = []
    if priority_band is not None:
        sql += " WHERE priority_band = %s"
        params.append(priority_band)
    sql += " ORDER BY total_score DESC, hypothesis_id ASC LIMIT %s"
    params.append(limit)

    with db.tx() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_score_from_row(r) for r in rows]


def rank_queue(limit: int = 50) -> list[tuple[str, int, str]]:
    """`(hypothesis_id, total_score, priority_band)`, best first.

    Deliberately unfiltered.  The band is returned so the caller can apply its
    own policy, and hiding the rejected and weak rows here would discard the
    one part of the ranking that says a research family should be terminated
    rather than refined an eighth time.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise QualityError(f"limit must be a positive int, got {limit!r}")
    with db.tx() as cur:
        cur.execute(
            "SELECT hypothesis_id, total_score, priority_band "
            "FROM hypothesis_quality ORDER BY total_score DESC, hypothesis_id ASC "
            "LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [(r["hypothesis_id"], int(r["total_score"]), r["priority_band"]) for r in rows]


# ---------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------
#
# The corpus is a tree, not a pile.  Each hypothesis is a node; each
# research_generations row is the edge that produced it, carrying what kind of
# change it was and why.  Without that, "this family has been revised seven
# times and still fails out of sample" cannot be asked at all -- and that
# question is the whole research finding: repeated refinement that keeps
# failing is positive evidence that the mechanism is wrong, not that the
# specification needs one more pass.


def _validate_mutation_type(mutation_type: str) -> str:
    if mutation_type not in MUTATION_TYPES:
        raise MutationTypeError(
            f"unknown mutation_type {mutation_type!r}; "
            f"expected one of {list(MUTATION_TYPES)}"
        )
    return mutation_type


def _generation_for(cur, hypothesis_id: str) -> dict[str, Any] | None:
    """The most recent generation that produced this hypothesis.

    A hypothesis may legitimately have several generations -- it can be
    revised twice -- and there is no unique constraint on `hypothesis_id`,
    correctly: uniqueness would reject valid history.  So "the" generation of
    a node means its latest one, chosen here rather than by a constraint that
    cannot be written.
    """
    cur.execute(
        f"""
        SELECT {_GENERATION_COLUMNS}
        FROM research_generations
        WHERE hypothesis_id = %s
        ORDER BY generation_number DESC, created_at DESC, generation_id DESC
        LIMIT 1
        """,
        (hypothesis_id,),
    )
    return cur.fetchone()


def record_generation(
    hypothesis_id: str,
    mutation_type: str,
    revision_reason: str,
    *,
    parent_hypothesis_id: str | None = None,
    critique_summary: str | None = None,
) -> Generation:
    """Record that `hypothesis_id` came from `parent_hypothesis_id`.

    `mutation_type` must be one of the seven; `revision_reason` must be
    non-empty, because a revision nobody can justify is a re-run, not progress.
    `generation_number` is derived -- a root is 1, otherwise one deeper than
    its parent -- so the tree depth is a fact rather than a claim.
    """
    _validate_mutation_type(mutation_type)
    if not isinstance(revision_reason, str) or not revision_reason.strip():
        raise MutationTypeError("revision_reason must be a non-empty string")

    generation_id = f"GEN_{uuid4().hex[:12]}"
    with db.tx() as cur:
        _require_hypothesis(cur, hypothesis_id)
        if parent_hypothesis_id is not None:
            _require_hypothesis(cur, parent_hypothesis_id)
            cur.execute(
                "SELECT max(generation_number) AS n FROM research_generations "
                "WHERE hypothesis_id = %s",
                (parent_hypothesis_id,),
            )
            parent_number = cur.fetchone()["n"]
            number = (int(parent_number) if parent_number is not None else 0) + 1
        else:
            number = 1
        cur.execute(
            f"""
            INSERT INTO research_generations (
                generation_id, hypothesis_id, parent_hypothesis_id,
                generation_number, mutation_type, revision_reason, critique_summary
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {_GENERATION_COLUMNS}
            """,
            (
                generation_id,
                hypothesis_id,
                parent_hypothesis_id,
                number,
                mutation_type,
                revision_reason.strip(),
                critique_summary,
            ),
        )
        row = cur.fetchone()
    return Generation.model_validate(row)


def list_generations(
    parent_hypothesis_id: str | None = None, limit: int = 100
) -> list[Generation]:
    """Recorded generations, most recent first.

    With no parent, every generation is listed: a corpus with lineage is only
    as good as its orphans are visible.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise QualityError(f"limit must be a positive int, got {limit!r}")
    sql = f"SELECT {_GENERATION_COLUMNS} FROM research_generations"
    params: list[Any] = []
    if parent_hypothesis_id is not None:
        sql += " WHERE parent_hypothesis_id = %s"
        params.append(parent_hypothesis_id)
    sql += " ORDER BY generation_number DESC, created_at DESC, generation_id DESC LIMIT %s"
    params.append(limit)

    with db.tx() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [Generation.model_validate(r) for r in rows]


def lineage(hypothesis_id: str) -> list[Generation]:
    """This hypothesis's ancestry, newest first, up to the root.

    Walks `parent_hypothesis_id` to the root and is cycle-safe by way of a
    visited set.  This system never writes a cycle -- the parent is recorded
    with the child, so a cycle cannot form -- but the table is readable and
    writable by anything holding a DSN, and a hand-inserted or restored
    cycle must not hang the process.  The set costs one hash per generation on
    a path that is a handful of rows deep, and it converts an unbounded walk
    into a bounded one.
    """
    chain: list[Generation] = []
    visited: set[str] = set()
    current = hypothesis_id
    while current is not None and current not in visited:
        visited.add(current)
        with db.tx() as cur:
            row = _generation_for(cur, current)
        if row is None:
            break
        generation = Generation.model_validate(row)
        chain.append(generation)
        current = generation.parent_hypothesis_id
    return chain


def family_of(hypothesis_id: str) -> list[str]:
    """Every hypothesis sharing this one's root ancestor, root first.

    Both recursive terms use UNION (deduplicating) rather than UNION ALL, and
    both deduplicate on the hypothesis id ALONE.  That is the cycle guard: a
    descendant set that carried a depth column would see `A,2` where it had
    already produced `A,0`, treat it as new, and recurse forever.  The system
    never writes a cycle, but this table is writable by anything holding a
    DSN, and an unbounded query is a much worse failure than a slightly odd
    ordering.

    A cycle has no root at all, so the root is chosen as the ancestor that no
    other generation points away from, falling back to the lexicographically
    smallest ancestor.  That fallback is arbitrary but total, so the answer is
    reproducible rather than dependent on row order.

    Ordering is by each node's own generation_number, which increases by one
    per step down a tree, so the root comes first and siblings are stable.
    """
    with db.tx() as cur:
        cur.execute(
            """
            WITH RECURSIVE ancestors AS (
                SELECT %s::text AS hypothesis_id
                UNION
                SELECT g.parent_hypothesis_id
                FROM research_generations g
                JOIN ancestors a ON g.hypothesis_id = a.hypothesis_id
                WHERE g.parent_hypothesis_id IS NOT NULL
            ),
            root AS (
                SELECT a.hypothesis_id
                FROM ancestors a
                ORDER BY
                    EXISTS (
                        SELECT 1 FROM research_generations g
                        WHERE g.hypothesis_id = a.hypothesis_id
                          AND g.parent_hypothesis_id IS NOT NULL
                    ),
                    a.hypothesis_id
                LIMIT 1
            ),
            family AS (
                SELECT hypothesis_id FROM root
                UNION
                SELECT g.hypothesis_id
                FROM research_generations g
                JOIN family f ON g.parent_hypothesis_id = f.hypothesis_id
            )
            SELECT f.hypothesis_id
            FROM family f
            LEFT JOIN LATERAL (
                SELECT max(generation_number) AS depth
                FROM research_generations g
                WHERE g.hypothesis_id = f.hypothesis_id
            ) g ON TRUE
            ORDER BY COALESCE(g.depth, 0), f.hypothesis_id
            """,
            (hypothesis_id,),
        )
        return [r["hypothesis_id"] for r in cur.fetchall()]


def refine_count(hypothesis_id: str) -> int:
    """How many times this research family has actually been revised.

    Counts generations whose mutation_type is not 'new', so it counts real
    revisions and not the family's founding nodes.  Seven is the number at
    which the family is not converging: continued refinement of the same
    mechanism is evidence against the mechanism.
    """
    family = family_of(hypothesis_id)
    if not family:
        return 0
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM research_generations "
            "WHERE hypothesis_id = ANY(%s) AND mutation_type <> 'new'",
            (family,),
        )
        return int(cur.fetchone()["n"])