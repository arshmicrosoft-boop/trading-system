"""Stage 3 -- Hypothesis Generator.

Turns one economic mechanism into a batch of falsifiable claims.

A hypothesis here is a *claim about behaviour*, never an instruction to
act.  That boundary is load-bearing rather than stylistic: an agent that
reasons about returns eventually writes a target into a field, and every
consumer downstream -- critic, queue, Layer 2 -- then inherits a
profitability objective this engine was deliberately built without.

So the generator decides only what could be *refuted*, and is told plainly
that a claim whose refutation it cannot name is not a hypothesis.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .. import ids, llm
from ..config import get_settings
from ..contracts import Hypothesis
from ..db import tx
from ..domains import ALL_VARIABLES, describe_domains
from . import extractor

__all__ = ["generate", "get_hypothesis", "list_hypotheses"]

#: Hard ceiling on one call.  Past roughly this many the generator stops
#: producing distinct refutable claims and starts padding, which is worse
#: than returning fewer.
MAX_HYPOTHESES_PER_CALL = 20

_WHITESPACE = re.compile(r"\s+")

#: Null hypotheses that name no observation at all.  A model that answers
#: "none" has just told us it cannot think of evidence against its own
#: claim -- the precise failure Layer 1 exists to catch, so the entry is
#: dropped rather than stored.
_NON_NULLS = frozenset(
    {
        "none",
        "n/a",
        "na",
        "tbd",
        "unknown",
        "never",
        "always",
        "not falsifiable",
        "cannot be falsified",
        "no null hypothesis",
    }
)

HYPOTHESIS_SYSTEM_PROMPT = """\
You are the Hypothesis Generator of a quantitative research engine.

A hypothesis is a FALSIFIABLE CLAIM about how markets behave. It is not a
strategy, not a recommendation, and not an instruction to anybody.

Every hypothesis you return must carry four fields:
  statement           the claim, in one sentence, phrased so that some
                      concrete observation could prove it wrong
  expected_mechanism  the economic reason the mechanism should exist: who
                      has to trade, why they are forced to, and why the
                      effect does not vanish before it can be measured
  candidate_variables observable variable names that could carry the claim
  null_hypothesis     the specific observation that would REFUTE the claim

Forbidden, in every field. Output containing these is discarded:
  * expected returns, profit targets, Sharpe targets, or any numeric target
  * position sizing, entries, exits, stops, or any instruction about what
    to trade, how much, or when
  * any statement of the form "this will make money"

Candidate variables must come from the variable pool supplied in the
request. You may combine supplied variables; you may not invent new
indicators.

Make each hypothesis distinct from the others: a restatement of the same
claim in different words is padding, not a hypothesis.

Reply with JSON only -- no prose, no commentary, no code fences -- in
exactly this shape:
{"hypotheses": [{"statement": "...",
                 "expected_mechanism": "...",
                 "candidate_variables": ["..."],
                 "null_hypothesis": "..."}]}
"""


def _normalize(text: str) -> str:
    """Comparison key for statements: case and spacing are not meaning."""
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _string_list(raw: Any) -> list[str]:
    """Ordered, de-duplicated strings, tolerating a bare string or junk."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _candidate_rows(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        rows = payload.get("hypotheses")
        return rows if isinstance(rows, list) else []
    return payload if isinstance(payload, list) else []


def _draft(item: Any, mechanism_id: str, taken: set[str]) -> dict[str, Any] | None:
    """Turn one model entry into keyword arguments, or None if unusable."""
    if not isinstance(item, dict):
        return None
    statement = item.get("statement")
    expected = item.get("expected_mechanism")
    null = item.get("null_hypothesis")
    if not isinstance(statement, str) or not isinstance(expected, str):
        return None
    statement = statement.strip()
    expected = expected.strip()
    null = null.strip() if isinstance(null, str) else ""
    if not statement or not expected:
        return None
    if _normalize(null) in _NON_NULLS:
        return None
    key = _normalize(statement)
    if key in taken:
        return None
    taken.add(key)
    return {
        "mechanism_id": mechanism_id,
        "statement": statement,
        "expected_mechanism": expected,
        "candidate_variables": _string_list(item.get("candidate_variables")),
    }


def _existing_keys(mechanism_id: str) -> set[str]:
    with tx() as cur:
        cur.execute(
            "SELECT statement FROM hypotheses WHERE mechanism_id = %s", (mechanism_id,)
        )
        return {_normalize(row["statement"]) for row in cur.fetchall()}


def _insert(
    cur,
    drafts: list[dict[str, Any]],
    model_name: str,
    prompt_version: str,
) -> list[Hypothesis]:
    """Persist the validated drafts, returning the stored rows.

    Each draft is put through the ``Hypothesis`` model before it reaches
    the database, so one malformed entry cannot poison the batch and the
    caller's returned list is exactly what the table holds.
    """
    stored: list[Hypothesis] = []
    now = datetime.now(timezone.utc)
    for draft in drafts:
        try:
            candidate = Hypothesis(
                hypothesis_id=ids.next_id(cur, "H"),
                status="proposed",
                generator_model=model_name,
                prompt_version=prompt_version,
                created_at=now,
                **draft,
            )
        except ValidationError:
            continue
        cur.execute(
            """
            INSERT INTO hypotheses
                (hypothesis_id, mechanism_id, statement, expected_mechanism,
                 candidate_variables, status, generator_model, prompt_version)
            VALUES (%s, %s, %s, %s, %s, 'proposed', %s, %s)
            RETURNING hypothesis_id, mechanism_id, statement, expected_mechanism,
                      candidate_variables, status, generator_model, prompt_version,
                      created_at
            """,
            (
                candidate.hypothesis_id,
                candidate.mechanism_id,
                candidate.statement,
                candidate.expected_mechanism,
                candidate.candidate_variables,
                candidate.generator_model,
                candidate.prompt_version,
            ),
        )
        stored.append(Hypothesis(**cur.fetchone()))
    return stored


def generate(mechanism_id: str, count: int = 5) -> list[Hypothesis]:
    """Generate falsifiable hypotheses for one mechanism.

    ``count`` is clamped rather than rejected: a caller asking for a
    hundred still gets a usable batch, and the model is never asked for a
    list long enough to turn into padding.

    The null hypothesis does not reach the formalizer as stored data --
    ``hypotheses`` has no column for it and the schema is fixed.  It is
    enforced here instead: an entry whose refutation condition is missing
    or tautological is dropped, so by the time a row exists its
    falsifiability has already been established.  The formalizer then asks
    the model for that hypothesis's refutation condition again when it
    writes ``HypothesisBlock.null``, which is where it is persisted.
    """
    mechanism = extractor.get_mechanism(mechanism_id)
    if mechanism is None:
        raise KeyError(f"mechanism {mechanism_id} not found")

    settings = get_settings()
    wanted = max(1, min(int(count), MAX_HYPOTHESES_PER_CALL))

    # The prompt sees what is already stored so the model aims at new
    # claims; the authoritative duplicate check happens inside the write
    # transaction, where a concurrent generator cannot slip a row past it.
    known = _existing_keys(mechanism_id)

    payload, model_name = llm.get_llm().complete_json(
        HYPOTHESIS_SYSTEM_PROMPT,
        json.dumps(
            {
                "task": "generate_hypotheses",
                "count": wanted,
                "mechanism": mechanism.model_dump(mode="json"),
                "variable_pool": {
                    "from_mechanism": mechanism.candidate_variables,
                    "global": list(ALL_VARIABLES),
                },
                "domains": describe_domains(),
                "existing_statements": sorted(known),
            },
            ensure_ascii=False,
        ),
    )

    taken = set(known)
    drafts: list[dict[str, Any]] = []
    for item in _candidate_rows(payload):
        if len(drafts) >= wanted:
            break
        draft = _draft(item, mechanism_id, taken)
        if draft is not None:
            drafts.append(draft)

    with tx() as cur:
        stored = _insert(cur, drafts, model_name, settings.prompt_version)
    if not stored:
        raise llm.LLMError(
            f"no usable hypotheses for mechanism {mechanism_id}: the model "
            "returned nothing that is both distinct and falsifiable"
        )
    return stored


def get_hypothesis(hypothesis_id: str) -> Hypothesis | None:
    with tx() as cur:
        cur.execute(
            """
            SELECT hypothesis_id, mechanism_id, statement, expected_mechanism,
                   candidate_variables, status, generator_model, prompt_version,
                   created_at
            FROM hypotheses
            WHERE hypothesis_id = %s
            """,
            (hypothesis_id,),
        )
        row = cur.fetchone()
    return Hypothesis(**row) if row else None


def list_hypotheses(status: str | None = None, limit: int = 100) -> list[Hypothesis]:
    with tx() as cur:
        cur.execute(
            """
            SELECT hypothesis_id, mechanism_id, statement, expected_mechanism,
                   candidate_variables, status, generator_model, prompt_version,
                   created_at
            FROM hypotheses
            WHERE (%(status)s::hypothesis_status IS NULL OR status = %(status)s::hypothesis_status)
            ORDER BY created_at DESC, hypothesis_id DESC
            LIMIT %(limit)s
            """,
            {"status": status, "limit": limit},
        )
        rows = cur.fetchall()
    return [Hypothesis(**row) for row in rows]