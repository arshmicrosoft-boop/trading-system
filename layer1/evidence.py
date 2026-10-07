"""The Evidence Ledger.

This is what makes the system a research engine rather than a strategy
chatbot.  A strategy chatbot ends at "and therefore we should trade X"; a
research engine has to be able to answer, months later and without asking
the model again, *where did that come from*, *was that measured or
asserted*, and *did anything disagree with it*.  Those three questions are
what this module makes answerable, and each of them is a reason the ledger
exists:

  1. **Typed claims.**  Every statement extracted from a source is filed
     with its epistemic type (see :mod:`layer1.contracts_ledger`).  Without
     it, an extraction bug -- or a model asserting a result it never read --
     is indistinguishable from evidence, forever.
  2. **Recorded contradictions.**  Two sources disagreeing is research
     output.  A flat pipeline silently keeps whichever note was written
     last, which discards the finding.
  3. **Point-in-time provenance.**  What a backtest may use is bounded by
     when content could first have been known, not by when it was
     published.

THE CLAIM-TYPE RULE, WHICH IS NOT A STYLE PREFERENCE
----------------------------------------------------
Four types, and the distinction is the whole point:

    fact             a state of the world; checkable against data
    author_claim     a source's assertion about a relationship
    empirical_result a measurement the source reports having made
    model_inference  OUR model's reasoning -- never literature

**Anything this system produced is ``model_inference``.**  Not
``empirical_result``, not ``fact``, not ``author_claim`` -- those three are
attributions to somebody else, and claiming them for our own output is a
false provenance claim.  ``empirical_result`` in particular is the trap: a
model asked to reason about a famous episode will produce a fluent, correct,
apparently-sourced "result" that is actually recall (the KTD-Fin failure
mode in ``knowledge/research_integrity/knowledge_leakage.md``).  The
contamination is invisible in the output text, so it cannot be filtered
afterwards -- it has to be prevented at the point of typing.  Hence three
enforcing mechanisms here:

  * ``claim_type`` is a REQUIRED POSITIONAL argument of :func:`record_claim`
    with no default, so a caller cannot omit it;
  * :func:`assert_claim_type` rejects a model-output path that claims
    source provenance;
  * :data:`MODEL_PRODUCED_CLAIM_TYPE` is the single constant to import.

A source's prestige is not a claim's truth: a peer-reviewed source can
contain an unverified assertion, and tier lives on the source precisely so
the two are weighed separately.

Two further prohibitions carried in from the source documents:

  * **Never average a contradiction.**  A hedged claim between two
    conflicting sources is falsifiable by neither, so it constrains nothing
    and launders a disagreement the corpus was supposed to keep.
  * **Never look ahead through metadata.**  See
    :func:`set_temporal_context`.

Nothing here touches returns, Sharpe or profitability.  The ledger measures
the quality of evidence, never the value of a trade.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import db
from .contracts_ledger import (
    CLAIM_TYPE_GUIDANCE,
    MODEL_PRODUCED_CLAIM_TYPE,
    SOURCE_EVIDENCE_CLAIM_TYPES,
    Claim,
    ClaimContradiction,
    ClaimContradictionIn,
    ClaimIn,
    ClaimType,
    SourceTemporalContext,
    SourceTemporalContextIn,
)

__all__ = [
    "CLAIM_TYPE_GUIDANCE",
    "CLAIM_ID_PREFIX",
    "MODEL_PRODUCED_CLAIM_TYPE",
    "SOURCE_EVIDENCE_CLAIM_TYPES",
    "EvidenceError",
    "assert_claim_type",
    "record_claim",
    "record_model_inference",
    "get_claim",
    "list_claims",
    "record_contradiction",
    "list_contradictions",
    "set_temporal_context",
    "get_temporal_context",
    "find_conflicts",
]

#: Prefix for minted claim identities.  Distinguishable at a glance from the
#: sequence-issued ``SRC_001`` / ``H012`` identifiers, which is the signal
#: that this table has no sequence behind it.
CLAIM_ID_PREFIX = "CLM"


class EvidenceError(ValueError):
    """The ledger was asked to record something it cannot record honestly.

    Raised for a missing referenced id, a self-contradiction, an incoherent
    point-in-time ordering, or a model-output path claiming source
    provenance.  These are refused rather than repaired: each one is a place
    where a plausible-looking stored row would be a false one.
    """


# ---------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------

_CLAIM_COLUMNS = (
    "claim_id, source_id, statement, claim_type, section_ref, "
    "evidence_strength, time_context, agent_interpretation, created_at"
)

_TEMPORAL_COLUMNS = (
    "source_id, publication_date, information_available_at, "
    "future_information_risk"
)


def _mint_claim_id() -> str:
    """Allocate a claim identity.

    Deliberately NOT a sequence.  ``db/schema.sql`` defines sequences for
    sources, mechanisms, hypotheses and experiments, but none exists for
    ``claims`` -- and adding one would mean editing a SQL file that is the
    schema of record and that this change is not permitted to touch.  A
    Postgres sequence would also buy very little here: it serialises every
    claim writer across worker processes for an ordering nobody consumes,
    while claims are minted concurrently by the extractor, the generator and
    ad-hoc research paths, and their identity only ever has to be unique.

    So the id is a random token.  The trade-off is accepted knowingly: claim
    ids are no longer sortable by age (order by ``created_at`` instead), and
    that is why the id carries a prefix -- a reader must be able to tell at a
    glance that this column is not sequence-backed.
    """
    return f"{CLAIM_ID_PREFIX}_{uuid4().hex[:12]}"


def _mint_contradiction_id() -> str:
    """Same reasoning as :func:`_mint_claim_id`; see it for why."""
    return f"CTR_{uuid4().hex[:12]}"


def _contradiction_columns(prefix: str = "") -> str:
    """Column list for `claim_contradictions`, with the array cast.

    `possible_reasons` is a `conflict_reason[]` -- an array of a Postgres
    ENUM, not of a builtin type.  psycopg3 has no loader registered for
    arrays of a custom enum, so it hands the column back as its raw text
    literal, '{market,regime}'.  Casting to `text[]` in SQL gets a real
    Python list, which is what the contract validates.  The cast lives here
    rather than in a type-caster registration because this is the only
    custom-enum array in the schema and a global caster for one column
    would be indirection the rest of the codebase cannot use.
    """
    p = f"{prefix}." if prefix else ""
    return (
        f"{p}contradiction_id, {p}claim_a, {p}claim_b, "
        f"{p}possible_reasons::text[] AS possible_reasons, "
        f"{p}resolution_status, {p}spawned_hypothesis_id, {p}created_at"
    )


def _check_distinct(claim_a: str, claim_b: str) -> None:
    """Refuse a claim set against itself.

    Checked here rather than left to the Pydantic model or the DB CHECK so
    the refusal is this module's `EvidenceError` with this module's
    explanation, rather than a validation dump or a constraint name.  The
    model keeps its own validator too: this is the call-site defence, that
    one is the defence against the model being constructed directly.
    """
    if claim_a == claim_b:
        raise EvidenceError(
            f"claim {claim_a!r} cannot contradict itself; that is a data "
            "error (a duplicated or mis-extracted claim), not a cross-source "
            "conflict, and filing it as research would mean analysing our "
            "own extraction bug forever"
        )


def _aware(value: datetime) -> datetime:
    """UTC-attach a naive timestamp so comparisons are never a guess.

    Same normalisation as the contract applies, done here as well because
    this comparison runs before the model is built -- comparing a naive and
    an aware datetime would raise a TypeError about tzinfo instead of the
    point-in-time message the caller needs.
    """
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _require_source(cur, source_id: str) -> None:
    """Refuse to attach evidence to a source that does not exist.

    The FK would catch this too, but an FK error names a constraint rather
    than the id the caller passed, and a research run that mis-spelled a
    source id would otherwise write a claim orphaned from the evidence it
    claims to quote.
    """
    if not source_id or not source_id.strip():
        raise EvidenceError("source_id is required")
    cur.execute("SELECT 1 FROM sources WHERE source_id = %s", (source_id,))
    if cur.fetchone() is None:
        raise EvidenceError(
            f"source {source_id!r} does not exist; claims are evidence about a "
            "stored source, so ingest the source first (source_collector.collect)"
        )


def _require_claim(cur, claim_id: str, role: str) -> None:
    """Name the missing claim id instead of letting the FK report it."""
    if not claim_id or not claim_id.strip():
        raise EvidenceError(f"{role} is required")
    cur.execute("SELECT 1 FROM claims WHERE claim_id = %s", (claim_id,))
    if cur.fetchone() is None:
        raise EvidenceError(
            f"claim {claim_id!r} ({role}) does not exist; a contradiction "
            "between a claim and nothing is not a finding"
        )


def _require_hypothesis(cur, hypothesis_id: str) -> None:
    if not hypothesis_id or not hypothesis_id.strip():
        raise EvidenceError("spawned_hypothesis_id is required")
    cur.execute(
        "SELECT 1 FROM hypotheses WHERE hypothesis_id = %s", (hypothesis_id,)
    )
    if cur.fetchone() is None:
        raise EvidenceError(
            f"hypothesis {hypothesis_id!r} does not exist; a contradiction may "
            "only name a hypothesis that was actually recorded"
        )


def _row_to_claim(row: dict[str, Any]) -> Claim:
    """Validate one ``claims`` row through the contract model.

    ``claim_type`` and ``possible_reasons`` are Postgres enums stored as
    text, which is exactly what the Literals expect.
    """
    return Claim.model_validate(dict(row))


def _row_to_contradiction(row: dict[str, Any]) -> ClaimContradiction:
    return ClaimContradiction.model_validate(dict(row))


def _row_to_temporal(row: dict[str, Any]) -> SourceTemporalContext:
    return SourceTemporalContext.model_validate(dict(row))


# ---------------------------------------------------------------------
# The claim-type rule
# ---------------------------------------------------------------------


def assert_claim_type(claim_type: str, *, produced_by_model: bool) -> str:
    """Reject a model-output path that claims source provenance.

    ``produced_by_model=True`` means the sentence was generated by our own
    LLM.  It may then only be typed ``model_inference``.  The check is here,
    in Python, because by the time a row exists the false attribution has
    already been made -- and a false attribution that reads like literature
    is the failure this engine exists to prevent.

    The converse is deliberately NOT enforced: a ``model_inference`` claim
    is not the only truthful kind, and refusing to let a model quote a
    source verbatim would be wrong.  What is refused is the model claiming
    to BE the source.
    """
    if produced_by_model and claim_type != MODEL_PRODUCED_CLAIM_TYPE:
        raise EvidenceError(
            f"claim_type={claim_type!r} attributed to model output; a claim "
            f"this system produced is {MODEL_PRODUCED_CLAIM_TYPE!r} and "
            "nothing else. Types "
            f"{sorted(SOURCE_EVIDENCE_CLAIM_TYPES)} describe what a SOURCE "
            "asserted or measured. Filing our own reasoning as one of them "
            "is the memorisation-wearing-analysis failure from KTD-Fin: the "
            "output looks right and the provenance is false. If the sentence "
            "really was read out of the source, record it from the source "
            "path with produced_by_model=False and a section_ref."
        )
    return claim_type


# ---------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------


def record_claim(
    source_id: str,
    statement: str,
    claim_type: ClaimType,
    *,
    section_ref: str | None = None,
    evidence_strength: str | None = None,
    time_context: dict[str, Any] | None = None,
    agent_interpretation: str | None = None,
) -> Claim:
    """File one typed statement extracted from ``source_id``.

    ``claim_type`` is required and positional.  There is no default,
    deliberately: the epistemic type of a sentence is the single most
    important thing about it, and a default would let a call site that
    forgot the question decide it implicitly.  ``empirical_result`` would be
    the dangerous default -- it reads as the strongest type and would
    silently absorb model output.

    ``section_ref`` is the locator into the source (page, section or
    timestamp).  Its absence is tolerated, because an extractor that cannot
    point at what it extracted should still be allowed to record the
    extraction, but a claim without one is visibly weaker provenance and
    that is the honest signal.

    Append-only: there is no update path here by design, and the database
    enforces it with a BEFORE UPDATE trigger.
    """
    assert_claim_type(claim_type, produced_by_model=False)
    payload = ClaimIn(
        source_id=source_id,
        statement=statement,
        claim_type=claim_type,
        section_ref=section_ref,
        evidence_strength=evidence_strength,
        time_context=time_context,
        agent_interpretation=agent_interpretation,
    )

    with db.tx() as cur:
        _require_source(cur, payload.source_id)
        claim_id = _mint_claim_id()
        cur.execute(
            "INSERT INTO claims (claim_id, source_id, statement, claim_type, "
            "section_ref, evidence_strength, time_context, "
            "agent_interpretation) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING "
            + _CLAIM_COLUMNS,
            (
                claim_id,
                payload.source_id,
                payload.statement,
                payload.claim_type,
                payload.section_ref,
                payload.evidence_strength,
                db.Jsonb(payload.time_context) if payload.time_context is not None else None,
                payload.agent_interpretation,
            ),
        )
        return _row_to_claim(cur.fetchone())


def record_model_inference(
    source_id: str,
    statement: str,
    *,
    section_ref: str | None = None,
    evidence_strength: str | None = None,
    time_context: dict[str, Any] | None = None,
    agent_interpretation: str | None = None,
) -> Claim:
    """Record a sentence OUR model produced.

    The one function a generator or critic may call for its own reasoning.
    It does not accept a ``claim_type`` at all: there is no version of this
    call that can produce literature evidence, which is the property worth
    having at the signature level rather than in a review comment.
    """
    return record_claim(
        source_id,
        statement,
        MODEL_PRODUCED_CLAIM_TYPE,
        section_ref=section_ref,
        evidence_strength=evidence_strength,
        time_context=time_context,
        agent_interpretation=agent_interpretation,
    )


def get_claim(claim_id: str) -> Claim | None:
    """Read one claim.  None when it does not exist."""
    if not claim_id or not claim_id.strip():
        raise EvidenceError("claim_id is required")
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE claim_id = %s",
            (claim_id,),
        )
        row = cur.fetchone()
    return _row_to_claim(row) if row is not None else None


def list_claims(
    source_id: str | None = None,
    claim_type: str | None = None,
    limit: int = 100,
) -> list[Claim]:
    """List claims, oldest first, optionally filtered.

    ``claim_type`` is the filter the whole design exists to support: asking
    separately for what a source asserted, what it measured, and what WE
    inferred is how a later reader avoids mistaking one for another.
    """
    if limit < 1:
        raise EvidenceError(f"limit must be >= 1, got {limit}")
    where: list[str] = []
    params: list[Any] = []
    if source_id is not None:
        where.append("source_id = %s")
        params.append(source_id)
    if claim_type is not None:
        where.append("claim_type = %s")
        params.append(claim_type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM claims{clause} "
            "ORDER BY created_at ASC, claim_id ASC LIMIT %s",
            (*params, limit),
        )
        rows = cur.fetchall()
    return [_row_to_claim(r) for r in rows]


# ---------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------


def record_contradiction(
    claim_a: str,
    claim_b: str,
    possible_reasons: list[str],
    *,
    spawned_hypothesis_id: str | None = None,
    resolution_status: str = "unresolved",
) -> ClaimContradiction:
    """Record that two claims disagree, and why they might.

    Three refusals, all deliberate:

    * **Both claims must exist.**  Otherwise a typo silently files a
      conflict against nothing, and the corpus reports disagreement it
      never had.
    * **A claim may not contradict itself.**  The DB CHECK enforces this,
      but a constraint violation is a poor explanation.  A self-contradiction
      is a duplicated or mis-extracted claim -- our bug -- and treating it as
      research would mean analysing our own extraction error forever.
    * **De-duplication returns the existing row.**  ``(claim_a, claim_b)``
      is UNIQUE, so re-recording the same conflict is idempotent rather than
      an error.  Note the ordering: the DB constraint is on the ordered pair,
      so ``(a, b)`` and ``(b, a)`` are different rows to Postgres.  The
      schema comment states the intent -- de-duplicate "regardless of which
      claim was seen first" -- so the lookup here is on the UNORDERED pair
      and both orientations return the row that already exists.

    ``possible_reasons`` must be an enumerated, non-empty list.
    ``knowledge/methods/contradiction_resolution.md`` is blunt about this: a
    conflict record saying "they used different assumptions" has recorded
    nothing, and the enumeration is what makes the conflict actionable
    rather than merely noted.

    Nothing is averaged, hedged or resolved here.  Conflict resolution is a
    Layer 2 job, and only data is entitled to settle an empirical
    disagreement.
    """
    # Checked before the model so the refusal is an EvidenceError naming the
    # claim, rather than a pydantic dump that buries the explanation.
    _check_distinct(claim_a, claim_b)
    payload = ClaimContradictionIn(
        claim_a=claim_a,
        claim_b=claim_b,
        possible_reasons=possible_reasons,
        spawned_hypothesis_id=spawned_hypothesis_id,
        resolution_status=resolution_status,
    )

    with db.tx() as cur:
        _require_claim(cur, payload.claim_a, "claim_a")
        _require_claim(cur, payload.claim_b, "claim_b")
        if payload.spawned_hypothesis_id is not None:
            _require_hypothesis(cur, payload.spawned_hypothesis_id)

        cur.execute(
            f"SELECT {_contradiction_columns()} FROM claim_contradictions "
            "WHERE (claim_a = %s AND claim_b = %s) OR (claim_a = %s AND claim_b = %s) "
            "LIMIT 1",
            (payload.claim_a, payload.claim_b, payload.claim_b, payload.claim_a),
        )
        existing = cur.fetchone()
        if existing is not None:
            # Idempotent by design: the pair is UNIQUE, so a second write is
            # a duplicate report of a conflict we already hold, not new
            # information.  The first recorded row wins, including its
            # resolution_status -- silently upgrading it here would let a
            # stale caller overwrite a resolution with 'unresolved'.
            return _row_to_contradiction(existing)

        contradiction_id = _mint_contradiction_id()
        cur.execute(
            "INSERT INTO claim_contradictions (contradiction_id, claim_a, "
            "claim_b, possible_reasons, resolution_status, "
            "spawned_hypothesis_id) VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING " + _contradiction_columns(),
            (
                contradiction_id,
                payload.claim_a,
                payload.claim_b,
                payload.possible_reasons,
                payload.resolution_status,
                payload.spawned_hypothesis_id,
            ),
        )
        return _row_to_contradiction(cur.fetchone())


def list_contradictions(
    resolution_status: str | None = None,
    limit: int = 100,
) -> list[ClaimContradiction]:
    """List recorded conflicts, newest first.

    Unresolved conflicts first in practice: pass no filter to get
    everything, or ``'unresolved'`` to get the corpus's open leads.
    """
    if limit < 1:
        raise EvidenceError(f"limit must be >= 1, got {limit}")
    clause = ""
    params: list[Any] = []
    if resolution_status is not None:
        clause = " WHERE resolution_status = %s"
        params.append(resolution_status)
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_contradiction_columns()} FROM claim_contradictions{clause} "
            "ORDER BY created_at DESC, contradiction_id DESC LIMIT %s",
            (*params, limit),
        )
        rows = cur.fetchall()
    return [_row_to_contradiction(r) for r in rows]


def find_conflicts(source_id: str, limit: int = 100) -> list[ClaimContradiction]:
    """Every conflict involving any claim of ``source_id``, in either position.

    This is how "Paper A says X, Paper B says not-X" is found.  The
    unordered join matters: a conflict is recorded once, by whichever claim
    was seen first, so searching only ``claim_a`` would silently lose half
    the conflicts depending on extraction order -- and the missing half is
    the half involving the source we just ingested.
    """
    if limit < 1:
        raise EvidenceError(f"limit must be >= 1, got {limit}")
    with db.tx() as cur:
        _require_source(cur, source_id)
        cur.execute(
            f"""
            SELECT DISTINCT {_contradiction_columns("cc")}
            FROM claim_contradictions cc
            WHERE cc.claim_a IN (
                      SELECT c.claim_id FROM claims c WHERE c.source_id = %s
                  )
               OR cc.claim_b IN (
                      SELECT c.claim_id FROM claims c WHERE c.source_id = %s
                  )
            ORDER BY cc.created_at DESC, cc.contradiction_id DESC
            LIMIT %s
            """,
            (source_id, source_id, limit),
        )
        rows = cur.fetchall()
    return [_row_to_contradiction(r) for r in rows]


# ---------------------------------------------------------------------
# Point-in-time provenance
# ---------------------------------------------------------------------


def set_temporal_context(
    source_id: str,
    information_available_at: datetime,
    *,
    publication_date: datetime | None = None,
    future_information_risk: str = "unknown",
) -> SourceTemporalContext:
    """Record when a source's content could first have been known.

    The point-in-time rule: **a source available at time T may not use
    information that only became available after T.**  A backtest is bounded
    by ``information_available_at``, not ``publication_date`` -- a December
    backtest read in February already knows things a December trader did
    not, and the lag between the two dates is exactly where look-ahead hides.

    So a ``publication_date`` AFTER ``information_available_at`` is refused.
    The incoherent ordering is not a formatting nit: reading it the other
    way is the only way to admit content earlier than it existed, and that is
    look-ahead arriving through **metadata** rather than through data -- the
    version that a data-side filter never catches.

    ``future_information_risk`` defaults to ``'unknown'`` because unknown is
    the honest state, and a risk never assessed must not read as absent.

    Upsert, not insert-only: this table is keyed by ``source_id`` and holds a
    *current assessment* rather than immutable evidence, so re-assessing a
    source is a legitimate write.  ``claims`` is the append-only table; this
    one is not, and keeping the distinction straight is why the two are
    separate tables at all.
    """
    # Checked before the model for the same reason as the self-contradiction
    # case: this module refuses with EvidenceError and a sentence a human can
    # act on, not a pydantic dump.  The model keeps its own validator as the
    # defence against the model being constructed directly.
    if (
        publication_date is not None
        and _aware(publication_date) > _aware(information_available_at)
    ):
        raise EvidenceError(
            f"publication_date {_aware(publication_date).isoformat()} is AFTER "
            f"information_available_at "
            f"{_aware(information_available_at).isoformat()}: content cannot "
            "have been knowable before it was published. Either the dates are "
            "transposed or information_available_at has been backdated to "
            "admit look-ahead -- and look-ahead that arrives through metadata "
            "is the version a data-side filter never catches. Fix the "
            "provenance rather than the check."
        )

    payload = SourceTemporalContextIn(
        source_id=source_id,
        publication_date=publication_date,
        information_available_at=information_available_at,
        future_information_risk=future_information_risk,
    )
    with db.tx() as cur:
        _require_source(cur, payload.source_id)
        cur.execute(
            "INSERT INTO source_temporal_context (source_id, "
            "publication_date, information_available_at, "
            "future_information_risk) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (source_id) DO UPDATE SET "
            "publication_date = EXCLUDED.publication_date, "
            "information_available_at = EXCLUDED.information_available_at, "
            "future_information_risk = EXCLUDED.future_information_risk "
            "RETURNING " + _TEMPORAL_COLUMNS,
            (
                payload.source_id,
                payload.publication_date,
                payload.information_available_at,
                payload.future_information_risk,
            ),
        )
        return _row_to_temporal(cur.fetchone())


def get_temporal_context(source_id: str) -> SourceTemporalContext | None:
    """Read a source's point-in-time provenance.

    None means either "no source" or "no assessment recorded", and those are
    deliberately not distinguished: a source with no temporal context is
    INCOMPLETE for any use beyond mechanism discovery, and reading that as
    "risk is fine" is precisely the failure the ``unknown`` default exists
    to prevent.  Callers that need the distinction should use
    :func:`get_source`.
    """
    if not source_id or not source_id.strip():
        raise EvidenceError("source_id is required")
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_TEMPORAL_COLUMNS} FROM source_temporal_context "
            "WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    return _row_to_temporal(row) if row is not None else None
