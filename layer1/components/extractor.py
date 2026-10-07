"""Stage 2 -- Research Extractor.

Reads a source and extracts the ECONOMIC MECHANISMS the evidence implies.

This is the module where the system's central discipline is enforced.  The
model is asked what *happens in the world* and why it should persist, never
what to trade.  A mechanism is a claim about a process -- "market makers
with unfilled inventory quote more aggressively near the close" -- from which
a falsifiable hypothesis can later be derived.  A trade recommendation is a
conclusion that cannot be falsified by observation, and Layer 1 has no
ground truth about outcomes to check one against.

Candidate variables come from explicit domain universes (`layer1.domains`)
rather than an open indicator buffet, because unbounded free association is
what produced thousands of shallow, mutually incomparable claims in the
predecessor system.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .. import db, domains, ids, llm
from ..config import get_settings
from ..contracts import Mechanism, MechanismRecord
from . import source_collector

__all__ = ["extract", "get_mechanism", "list_mechanisms"]

#: Below this many words there is no claim to extract, and a model handed a
#: sentence will happily invent one to fill the JSON.  Refuse instead.
MIN_WORDS = 40

_MECHANISM_COLUMNS = (
    "mechanism_id, source_id, mechanism_name, observation, "
    "mechanism_description, candidate_variables, domain, "
    "extraction_model, prompt_version, created_at"
)

_SYSTEM_PROMPT = f"""\
You are the Research Extractor for a quantitative research engine.

You read one piece of evidence and report the ECONOMIC MECHANISMS it
implies. A mechanism is a causal process already visible or credibly implied
by the evidence: a structural feature of the market, a constraint on
behaviour, a pressure that transmits from one participant to another.

You are strictly forbidden from producing any of the following. This is a
hard boundary, not a preference:
  * trade recommendations, or any "buy", "sell", "long", "short" instruction;
  * entry or exit timing, price levels, or order instructions;
  * position sizing, capital allocation, or leverage guidance;
  * claims about what an approach is worth or what it would have earned;
  * restatements of a trading strategy already described in the evidence.

If the evidence is itself a trading strategy or a promotional claim, report
only the underlying structural mechanism it presupposes. If no such mechanism
exists, return an empty "mechanisms" list. An empty list is a valid and
useful answer; an invented mechanism is not.

The research universes below are the bounded spaces you may ground
candidates in. Prefer their variables. You may propose a variable outside
them, but only when you state in "mechanism_description" why it belongs to
this mechanism.

{domains.describe_domains()}

Return JSON only -- no prose, no markdown fence -- with exactly this shape:

{{"mechanisms": [
  {{
    "mechanism_name": "short kebab or Title Case label for the mechanism",
    "observation": "what the source literally shows, stated as fact about "
                   "the evidence, with no interpretation",
    "mechanism_description": "why this process should persist and continue "
                             "to act, including who is constrained and how",
    "candidate_variables": ["variable_name", "..."],
    "domain": "one of the keys above, or null"
  }}
]}}

Both "observation" and "mechanism_description" are required and must not be
restatements of each other. At most 8 mechanisms; fewer is better than
padding.
"""


def _build_user_payload(source_id: str, title: str, source_type: str, text: str) -> dict[str, Any]:
    """Prompt payload.

    A JSON document, not prose: the deterministic provider reads its answer
    from a `__response__` key in exactly this document, so tests can inject
    canned output by patching this function or the LLM client.
    """
    return {
        "source_id": source_id,
        "source_type": source_type,
        "title": title,
        "content": text,
    }


def _project(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only the known mechanism fields.

    Real models add keys of their own (`confidence`, `notes`). Dropping an
    otherwise well-formed mechanism because of a stray field would lose good
    research, while a missing or empty *required* field is still rejected by
    `Mechanism` below.
    """
    return {
        "mechanism_name": entry.get("mechanism_name"),
        "observation": entry.get("observation"),
        "mechanism_description": entry.get("mechanism_description"),
        "candidate_variables": entry.get("candidate_variables"),
        "domain": entry.get("domain"),
    }


def _row_to_mechanism(row: dict[str, Any]) -> MechanismRecord:
    return MechanismRecord.model_validate(dict(row))


def extract(source_id: str, max_mechanisms: int = 8) -> list[MechanismRecord]:
    """Extract mechanisms from a stored source and persist them."""
    if not source_id:
        raise ValueError("source_id is required")
    if max_mechanisms < 1:
        raise ValueError(f"max_mechanisms must be >= 1, got {max_mechanisms}")

    source = source_collector.get_source(source_id)
    if source is None:
        raise ValueError(f"source {source_id!r} does not exist; collect it first")
    if source.word_count < MIN_WORDS:
        raise ValueError(
            f"source {source_id!r} has only {source.word_count} words "
            f"({MIN_WORDS} required); there is not enough content to extract a "
            "mechanism from, and extracting one anyway would be fabrication"
        )

    settings = get_settings()
    user = json.dumps(
        _build_user_payload(
            source.source_id, source.title, source.source_type, source.content
        ),
        ensure_ascii=False,
    )

    parsed, model_name = llm.get_llm().complete_json(_SYSTEM_PROMPT, user)

    raw_entries = parsed.get("mechanisms") if isinstance(parsed, dict) else None
    if not isinstance(raw_entries, list):
        raise llm.LLMError(
            f"extractor returned no 'mechanisms' list for source {source_id!r}; "
            f"got {type(parsed).__name__} with keys "
            f"{sorted(parsed) if isinstance(parsed, dict) else 'n/a'}"
        )

    raw_entries = [e for e in raw_entries if isinstance(e, dict)][:max_mechanisms]

    mechanisms: list[Mechanism] = []
    rejected: list[str] = []
    for entry in raw_entries:
        try:
            mechanisms.append(Mechanism.model_validate(_project(entry)))
        except ValidationError as exc:
            # One malformed entry is a bad entry, not a bad extraction: the
            # remaining mechanisms are still supported by the evidence.
            name = entry.get("mechanism_name", "<unnamed>")
            rejected.append(f"{name!r}: {exc.error_count()} field error(s)")

    if not mechanisms:
        raise llm.LLMError(
            f"extractor produced no valid mechanism for source {source_id!r} "
            f"(model={model_name}); {len(raw_entries)} entry/entries rejected -- "
            + ("; ".join(rejected) if rejected else "the model returned an empty list")
        )

    with db.tx() as cur:
        records: list[MechanismRecord] = []
        for mech in mechanisms:
            mechanism_id = ids.next_id(cur, "MECH")
            cur.execute(
                "INSERT INTO research_notes (mechanism_id, source_id, "
                "mechanism_name, observation, mechanism_description, "
                "candidate_variables, domain, extraction_model, prompt_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING " + _MECHANISM_COLUMNS,
                (
                    mechanism_id,
                    source.source_id,
                    mech.mechanism_name,
                    mech.observation,
                    mech.mechanism_description,
                    list(mech.candidate_variables),
                    mech.domain,
                    model_name,
                    settings.prompt_version,
                ),
            )
            records.append(_row_to_mechanism(cur.fetchone()))
    return records


def get_mechanism(mechanism_id: str) -> MechanismRecord | None:
    """Read one mechanism.  None when it does not exist."""
    if not mechanism_id:
        raise ValueError("mechanism_id is required")
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_MECHANISM_COLUMNS} FROM research_notes WHERE mechanism_id = %s",
            (mechanism_id,),
        )
        row = cur.fetchone()
    return _row_to_mechanism(row) if row is not None else None


def list_mechanisms(source_id: str | None = None) -> list[MechanismRecord]:
    """Mechanisms in extraction order, optionally scoped to one source."""
    with db.tx() as cur:
        if source_id is None:
            cur.execute(
                f"SELECT {_MECHANISM_COLUMNS} FROM research_notes "
                "ORDER BY mechanism_id"
            )
        else:
            cur.execute(
                f"SELECT {_MECHANISM_COLUMNS} FROM research_notes "
                "WHERE source_id = %s ORDER BY mechanism_id",
                (source_id,),
            )
        rows = cur.fetchall()
    return [_row_to_mechanism(r) for r in rows]