"""Stage 5: the Research Critic.

The generator's job is to produce candidates; the critic's job is to kill
them.  A critic that agrees with everything it reads is worse than no critic,
because it converts the absence of review into the appearance of review, and
Layer 2 then spends real compute re-deriving what a hostile reading would have
rejected for free.

Two design commitments follow from that, and both are enforced in code rather
than requested in a prompt:

* **The model's verdict is advisory; the deterministic floor is not.**  A
  language model that is told to be hostile is still a language model, and it
  will cheerfully return PASS on a hypothesis that scores zero for
  measurability or future-information leakage.  Those two dimensions are
  overridden unconditionally (see `_FLOOR_OVERRIDES`).
* **Silence is not soundness.**  A dimension the model failed to score is
  recorded as 0, not as "unknown".  Under fail-open scoring a model can pass a
  hypothesis by simply omitting the dimensions it dislikes.

Critiques are not persisted.  Layer 1 keeps the *outcome* of a review
(`failures` rows for FAILs, an `experiment_queue` row for a PASS) and not the
transcript; see `list_critiques`.
"""

from __future__ import annotations

import json
from typing import Any, get_args

from .. import db, ids
from ..config import get_settings
from ..contracts import Critique, ExperimentJob, FailureType
from ..llm import get_llm

__all__ = [
    "CRITIC_SYSTEM_PROMPT",
    "FAILURE_TYPES",
    "critique",
    "apply_verdict",
    "list_critiques",
]

FAILURE_TYPES: tuple[str, ...] = get_args(FailureType)

#: Every dimension the contract requires, in the order the prompt presents
#: them.  A score outside this set is dropped: an unrecognised key cannot
#: contribute to a verdict and would only dilute the transcript.
_DIMENSIONS: tuple[str, ...] = (
    "mechanism_plausible",
    "measurable",
    "falsifiable",
    "distinct",
    "no_hidden_discretion",
    "no_future_leakage",
    "no_prior_failure_collision",
)

#: Dimensions where a score of 0 overrides the model's own verdict.
#:
#: These two are not opinions.  A claim whose features cannot be computed from
#: data a backtester holds cannot be tested at all, and a claim that reads
#: information unavailable at decision time will always "work" -- that is what
#: leakage is.  Neither failure is repairable by a better prompt or a more
#: charitable reading, and both are cheap to detect, so they are decided here
#: rather than delegated.  A model that answers PASS while scoring these 0 is
#: exactly the failure mode this floor exists to catch.
_FLOOR_OVERRIDES: tuple[tuple[str, str], ...] = (
    # Leakage first: it is the more damning of the two, and when a hypothesis
    # is both unmeasurable and leaking, the leak is the reason to remember it.
    ("no_future_leakage", "future_information_leak"),
    ("measurable", "not_measurable"),
)

CRITIC_SYSTEM_PROMPT = f"""\
You are the Research Critic for an automated trading-research engine. Your job
is to REJECT hypotheses, not to agree with them.

Adopt the prior that the hypothesis in front of you is probably wrong. Assume
the economic story is post-hoc, that the features were chosen after seeing the
outcome, and that someone is about to waste a backtest on it. Your value is
entirely in finding the reason to say no; a PASS you issue by default is a
defect. Only pass a hypothesis you actively tried to break and could not.

Score every one of these seven dimensions 0-2, where 0 = fails, 1 = weak, 2 =
sound. Use the whole range. A hypothesis that is merely unremarkable is a 1,
not a 2, and a 2 must mean you looked for the defect and did not find it.

  mechanism_plausible        Is the economic story coherent? Would a
                              market participant force this trade, or is the
                              story a label invented after the fact?
  measurable                 Can every feature be computed from data a
                              backtester actually has at the stated horizon?
                              "Volume imbalance" is not measurable until the
                              book, clock, and sampling frequency are pinned.
  falsifiable                Is there a concrete observation that would
                              refute this? A claim that can only ever be
                              confirmed is not a hypothesis.
  distinct                   Is this a genuinely different claim, not
                              indicator soup and not a reworded sibling of
                              something already in the queue?
  no_hidden_discretion       Is every condition in the signal mechanical? Any
                              "when momentum looks strong", "if liquidity is
                              poor", or free parameter tuned by looking at
                              results is hidden discretion and must score 0.
  no_future_leakage          Does the rule use only information available at
                              the moment the decision is made? This is the
                              highest-value check you make: look for centred
                              windows, whole-sample statistics, revised or
                              restated data, survivorship, publication lag,
                              and any feature whose lookback reaches past the
                              decision timestamp. A leaking hypothesis is
                              worthless no matter how good its story is.
  no_prior_failure_collision Does this repeat something already refuted? The
                              prior_failures block lists what has already been
                              killed; near-duplicates of those must score 0.

Reply with a single JSON object and nothing else -- no prose, no markdown
fence, no commentary before or after. This exact shape:

{{
  "scores": {{
    "mechanism_plausible": <int 0-2>,
    "measurable": <int 0-2>,
    "falsifiable": <int 0-2>,
    "distinct": <int 0-2>,
    "no_hidden_discretion": <int 0-2>,
    "no_future_leakage": <int 0-2>,
    "no_prior_failure_collision": <int 0-2>
  }},
  "verdict": "<PASS or FAIL>",
  "failure_type": <null, or one string from the list below>,
  "reason": "<your own words naming the specific defect in THIS hypothesis>",
  "suggestions": ["<a concrete change that would make this testable, or an empty list>"]
}}

`failure_type` must be null when the verdict is PASS, and exactly one of these
when it is FAIL: {", ".join(FAILURE_TYPES)}.

`reason` must name the specific defect in the hypothesis you were actually
given, quoting its own condition or variable names. A vague reason such as
"the mechanism is weak" is a failed critique. Do not restate this
instruction, and do not reuse phrasing from any example -- an identical
`reason` across two different hypotheses means you did not read the second
one.
"""


# ---------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------


class CriticInputError(ValueError):
    """The critic cannot judge what it was given."""


def _load_hypothesis(hypothesis_id: str):
    """Stage 3 output, imported lazily.

    Imported inside the function rather than at module scope on purpose: the
    pipeline stages form a DAG, and a module-level import here would make
    importing this module depend on stages 3 and 4 being importable, which in
    turn blocks any future reverse edge (re-critiquing on a re-formalization)
    without a cycle-breaking import shim.
    """
    from . import generator

    hypothesis = generator.get_hypothesis(hypothesis_id)
    if hypothesis is None:
        raise CriticInputError(f"unknown hypothesis {hypothesis_id!r}")
    return hypothesis


def _load_spec(hypothesis_id: str):
    """The formalized spec, which is what the critic actually judges.

    Importing lazily for the same reason as `_load_hypothesis`.
    """
    from . import formalizer

    spec = formalizer.get_spec(hypothesis_id)
    if spec is None:
        # Judging measurability or leakage of an informalized claim is
        # impossible: both are properties of a concrete, evaluated condition.
        # Refusing here is better than letting the model grade prose.
        raise CriticInputError(
            f"hypothesis {hypothesis_id} has no formalized spec; "
            "the critic judges a rule, not a claim -- run formalizer.formalize() first"
        )
    return spec


def _prior_failure_digest(mechanism_id: str, limit: int = 25) -> dict[str, Any]:
    """Collect what is already known to be false, before asking the model.

    Ordering the query so that failures sharing this mechanism come first: the
    critic's seventh dimension is only meaningful if the collisions are at the
    top of the context rather than buried at the bottom.
    """
    with db.tx() as cur:
        cur.execute(
            """
            SELECT f.failure_id, f.hypothesis_id, f.failure_type, f.stage,
                   f.reported_by, left(f.reason, 300) AS reason,
                   (h.mechanism_id = %s) AS same_mechanism
            FROM failures f
            JOIN hypotheses h ON h.hypothesis_id = f.hypothesis_id
            ORDER BY same_mechanism DESC, f.created_at DESC, f.failure_id DESC
            LIMIT %s
            """,
            (mechanism_id, limit),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE stage = 'critic') AS from_critic "
            "FROM failures"
        )
        totals = cur.fetchone()

    return {
        "same_mechanism": [r for r in rows if r["same_mechanism"]],
        "other_mechanisms": [r for r in rows if not r["same_mechanism"]],
        "total_failures": int(totals["total"]),
        "from_critic": int(totals["from_critic"]),
    }


def _normalize_scores(raw: Any) -> dict[str, int]:
    """Coerce the model's score block to ints on the 0-2 scale.

    Missing, non-numeric, and out-of-range values all become 0.  That is the
    fail-closed reading, and it is deliberate: a model that omits a dimension
    has not established that the dimension is sound, it has declined to say.
    """
    if not isinstance(raw, dict):
        return {dim: 0 for dim in _DIMENSIONS}
    scores: dict[str, int] = {}
    for dim in _DIMENSIONS:
        try:
            value = int(raw[dim])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            value = 0
        scores[dim] = value if value in (0, 1, 2) else 0
    return scores


def _apply_deterministic_floor(
    scores: dict[str, int], verdict: str, failure_type: str | None
) -> tuple[str, str | None, list[str]]:
    """Override the model where the scores are indefensible.

    See `_FLOOR_OVERRIDES`.  Returns the possibly-corrected (verdict,
    failure_type, notes) triple.
    """
    notes: list[str] = []
    for dimension, forced_type in _FLOOR_OVERRIDES:
        if scores.get(dimension) != 0:
            continue
        if verdict != "FAIL" or failure_type != forced_type:
            notes.append(
                f"deterministic floor: {dimension}=0 forces verdict=FAIL "
                f"with failure_type={forced_type}"
            )
        verdict, failure_type = "FAIL", forced_type
        # Precedence: _FLOOR_OVERRIDES is ordered most-damaging first, so the
        # first hit decides.  Without this the loop would let the last
        # iteration overwrite the first and quietly relabel a leaking
        # hypothesis as merely unmeasurable -- the softer, less actionable of
        # the two diagnoses.
        break
    return verdict, failure_type, notes

def _research_identity_for(hypothesis_id: str, spec: Any) -> dict[str, Any]:
    """Mint (or recover) the provenance tuple that `enqueue` requires.

    The critic owns this row because it is the last stage before the boundary
    and the queue cannot exist without it.  Two subtleties make it more than a
    copy:

    * `research_id` is derived from the `spec_sha256` ALREADY STORED on the
      hypothesis_versions row -- never from `spec.spec_sha256()`.  `RuleSpec
      .spec_sha256()` is inherited by `RuleSpecRecord` unchanged, so hashing the
      record the formalizer hands back folds `version_id`, `model_used` and
      `creation_timestamp` into the digest and yields an id that matches
      nothing.  Reading the stored hash keeps the critic's derivation identical
      to the formalizer's, which is what makes the identity content-addressed
      across re-runs rather than per-call.
    * `data_version` is written as the Layer 1 sentinel.  Layer 1 has no data
      access and cannot pin a dataset; Layer 2 pins it when it claims the job.
    """
    from .queueing import PENDING_DATA_VERSION

    with db.tx() as cur:
        cur.execute(
            """
            SELECT version_id, spec_sha256, model_used, prompt_version
            FROM hypothesis_versions
            WHERE hypothesis_id = %s
            ORDER BY version_id DESC
            LIMIT 1
            """,
            (hypothesis_id,),
        )
        version_row = cur.fetchone()
        if version_row is None:
            raise CriticInputError(
                f"hypothesis {hypothesis_id} has no formalized version; "
                "cannot mint provenance"
            )

        # rule_version is the text spelling of version_id, per the stage-4
        # convention: the queue row carries both, and pinning them to one
        # another means they cannot drift.
        rule_version = str(version_row["version_id"])
        research_id = ids.research_id(hypothesis_id, version_row["spec_sha256"])
        cur.execute(
            "SELECT research_id FROM research_identity WHERE research_id = %s",
            (research_id,),
        )
        if cur.fetchone() is None:
            cur.execute(
                """
                SELECT n.source_id
                FROM hypotheses h
                JOIN research_notes n ON n.mechanism_id = h.mechanism_id
                WHERE h.hypothesis_id = %s
                """,
                (hypothesis_id,),
            )
            note = cur.fetchone()
            if note is None:
                raise CriticInputError(
                    f"hypothesis {hypothesis_id} has no originating research note"
                )
            cur.execute(
                """
                INSERT INTO research_identity (
                    research_id, hypothesis_id, source_ids, feature_set,
                    rule_version, data_version, parameter_space,
                    model_used, prompt_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (research_id) DO NOTHING
                """,
                (
                    research_id,
                    hypothesis_id,
                    [note["source_id"]],
                    list(spec.features),
                    rule_version,
                    PENDING_DATA_VERSION,
                    db.Jsonb({}),
                    version_row["model_used"],
                    version_row["prompt_version"],
                ),
            )

    return {
        "research_id": research_id,
        "version_id": version_row["version_id"],
        "feature_set": list(spec.features),
        "rule_version": rule_version,
    }


def _build_critique(
    hypothesis_id: str, payload: Any, critic_model: str
) -> tuple[Critique, list[str]]:
    """Turn raw model output into a validated `Critique`.

    `Critique` carries a `model_validator` that rejects a FAIL without a
    failure_type and a PASS carrying one.  That validator is the arbiter of
    coherence and is deliberately not worked around here: silently repairing
    incoherent critic output would turn a broken reviewer into an apparently
    working one.  A rejected payload is surfaced as a `ValueError` naming the
    hypothesis so the operator sees which review produced nonsense.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"critic for {hypothesis_id} returned {type(payload).__name__}, expected a JSON object"
        )

    scores = _normalize_scores(payload.get("scores"))
    raw_verdict = payload.get("verdict")
    verdict = raw_verdict if raw_verdict in ("PASS", "FAIL") else "FAIL"
    failure_type = payload.get("failure_type") if verdict == "FAIL" else None

    verdict, failure_type, floor_notes = _apply_deterministic_floor(
        scores, verdict, failure_type
    )

    raw_reason = payload.get("reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else ""
    if floor_notes and not reason:
        reason = floor_notes[0]
    suggestions = [
        s for s in (payload.get("suggestions") or []) if isinstance(s, str)
    ]

    try:
        critique = Critique(
            hypothesis_id=hypothesis_id,
            verdict=verdict,
            scores=scores,
            failure_type=failure_type,
            reason=reason or "(no reason supplied by the critic)",
            suggestions=suggestions + floor_notes,
            critic_model=critic_model,
            prompt_version=get_settings().prompt_version,
        )
    except ValueError as exc:
        raise ValueError(f"critic returned an incoherent verdict for {hypothesis_id}: {exc}") from exc
    return critique, floor_notes


def _set_hypothesis_status(hypothesis_id: str, status: str) -> None:
    """Advance the lifecycle column.  The enum is cast in SQL, not trusted."""
    with db.tx() as cur:
        cur.execute(
            "UPDATE hypotheses SET status = %s::hypothesis_status, updated_at = now() "
            "WHERE hypothesis_id = %s",
            (status, hypothesis_id),
        )


# ---------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------


def _has_reason(payload: Any) -> bool:
    """True when the model supplied a non-empty `reason` string."""
    if not isinstance(payload, dict):
        return False
    value = payload.get("reason")
    return isinstance(value, str) and bool(value.strip())


def critique(hypothesis_id: str) -> Critique:
    """Run the hostile review of one formalized hypothesis.

    Reads the prior failure record first and feeds it to the model, so
    `no_prior_failure_collision` is scored against what is actually known to
    be false rather than against the model's recollection.

    Nothing is persisted here.  `apply_verdict` is the writing half, and
    keeping them separate means a caller can re-read a verdict (or review it)
    without a database write having happened.
    """
    settings = get_settings()
    hypothesis = _load_hypothesis(hypothesis_id)
    spec = _load_spec(hypothesis_id)
    prior = _prior_failure_digest(hypothesis.mechanism_id)

    # The user message is a JSON document on purpose: the deterministic
    # provider parses it and reads its `__response__`, so offline runs and
    # tests drive the same code path as a real model.
    user_payload: dict[str, Any] = {
        "hypothesis": hypothesis.model_dump(mode="json"),
        "rule_spec": spec.model_dump(mode="json"),
        "prior_failures": prior,
        "prompt_version": settings.prompt_version,
    }

    parsed, critic_model = get_llm().complete_json(
        CRITIC_SYSTEM_PROMPT, json.dumps(user_payload, default=str)
    )

    # A verdict with no reasoning is not a usable research record: the reason
    # is the only thing that later explains WHY a hypothesis was queued or
    # rejected, and a silent PASS teaches nobody anything. Small models
    # routinely drop the field, so retry once with it named explicitly rather
    # than storing "(no reason supplied)". One retry only -- a model that
    # cannot produce a reason twice is better recorded as defective than
    # retried forever.
    if not _has_reason(parsed):
        retry_payload = dict(user_payload)
        retry_payload["_reminder"] = (
            "Your previous reply omitted `reason`. Include it: one or two "
            "sentences naming the specific defect (for FAIL) or the specific "
            "reason this hypothesis survived your scrutiny (for PASS)."
        )
        parsed, critic_model = get_llm().complete_json(
            CRITIC_SYSTEM_PROMPT, json.dumps(retry_payload, default=str)
        )

    critique_obj, _notes = _build_critique(hypothesis_id, parsed, critic_model)
    return critique_obj


def apply_verdict(critique: Critique) -> ExperimentJob | None:
    """Turn a verdict into its persistent outcome.

    PASS -> enqueue for Layer 2 and return the job.  FAIL -> record the
    failure as negative knowledge and reject the hypothesis, returning None.

    The status transition is deliberately ordered: a PASSed hypothesis is
    marked `critiqued` first, and `enqueue` promotes it to `queued`.  If the
    enqueue is refused -- a stale critic failure, a broken provenance triple --
    the hypothesis is left at `critiqued`, which is the truthful state (the
    critic ruled on it; it was not admitted).  The reverse order would have to
    claim `queued` for a job that does not exist.
    """
    from . import queueing

    if critique.verdict == "PASS":
        spec = _load_spec(critique.hypothesis_id)
        provenance = _research_identity_for(critique.hypothesis_id, spec)
        _set_hypothesis_status(critique.hypothesis_id, "critiqued")
        return queueing.enqueue(
            hypothesis_id=critique.hypothesis_id,
            version_id=provenance["version_id"],
            research_id=provenance["research_id"],
            feature_set=list(provenance["feature_set"]),
            rule_version=provenance["rule_version"],
        )

    queueing.record_failure(
        hypothesis_id=critique.hypothesis_id,
        reason=critique.reason,
        failure_type=critique.failure_type,
        stage="critic",
        reported_by="layer1.research_critic",
    )
    _set_hypothesis_status(critique.hypothesis_id, "rejected")
    return None


def list_critiques(hypothesis_id: str) -> list[Critique]:
    """Always raises.

    There is no `critiques` table, and one is deliberately not added.  The
    durable outcomes are already first-class rows -- an `experiment_queue` entry
    for a PASS, a `failures` entry for a FAIL -- and both carry the reason.
    A transcript table would add a fourth write path holding a copy of the
    same verdict, with nothing reading it and nothing to reconcile it against.

    To keep one: run `critique` again.  The review is a pure function of the
    stored hypothesis, spec, and failure record.
    """
    raise NotImplementedError(
        "Layer 1 does not persist critique transcripts; it persists verdict "
        "outcomes (experiment_queue rows for a PASS, failures rows for a FAIL, "
        f"both carrying the reason). Read those instead: "
        f"queueing.list_queue() / queueing.list_failures(hypothesis_id={hypothesis_id!r})."
    )
