"""Stage 1 -- Source Collector.

Sources are immutable evidence.  The database enforces that with a
BEFORE UPDATE trigger; this module simply never attempts a mutation, and
re-ingesting identical evidence is a no-op rather than a duplicate row.

De-duplication is on `content_sha256` rather than on (url, title): the same
paper fetched from two URLs is the same evidence, and a pipeline that is
re-run must not fork it into two sources that then generate two sets of
mechanisms.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .. import db, ids
from ..config import get_settings
from ..contracts import Source, SourceIn

__all__ = [
    "DEFAULT_TIER_BY_TYPE",
    "infer_tier",
    "collect",
    "get_source",
    "list_sources",
]

#: Default evidentiary tier per source type.
#:
#: Tier is *provenance*, not a judgement about whether the content is any
#: good: 1 peer-reviewed, 2 practitioner, 3 blog/news, 4 social media,
#: 5 LLM-generated.  `pdf` defaults to 1 because a PDF in this pipeline is
#: a paper; `youtube` defaults to 4 because a video claim carries the
#: weakest standing we accept; `manual` is the operator's own idea, which is
#: deliberate first-party thinking rather than third-party evidence.
DEFAULT_TIER_BY_TYPE: dict[str, int] = {
    "youtube": 4,
    "pdf": 1,
    "github": 2,
    "article": 3,
    "note": 2,
    "manual": 2,
}

#: Last-resort tier when a type is not in the table above.
_FALLBACK_TIER = 5

_SOURCE_COLUMNS = (
    "source_id, source_type, title, author, url, content, "
    "content_sha256, date, tier, ingested_at"
)


def infer_tier(source_type: str, author: str | None = None) -> int:
    """Return the default tier for `source_type`.

    `author` is accepted for provenance, not for promotion.  An academic-looking
    name does not upgrade a YouTube video's tier: tier records where the
    evidence came from, and silently rewriting it on a name heuristic would
    make the stored tier a guess dressed as a fact.  Callers who know the real
    provenance pass `SourceIn.tier` explicitly.
    """
    tier = DEFAULT_TIER_BY_TYPE.get(source_type)
    if tier is None:
        # An unknown type means we could not classify the provenance, which is
        # weaker than any known source, not stronger.
        return _FALLBACK_TIER
    return tier


def _validate_tier(tier: int) -> int:
    if not isinstance(tier, int) or isinstance(tier, bool):
        raise ValueError(f"tier must be an integer, got {type(tier).__name__}")
    if not 1 <= tier <= 5:
        raise ValueError(f"tier must be in 1..5, got {tier}")
    return tier


def _resolve_tier(source: SourceIn) -> int:
    """Explicit tier, else the deployment default, else the type default."""
    explicit = source.tier
    if explicit is not None:
        return _validate_tier(explicit)
    default_tier = get_settings().default_tier
    if default_tier is not None:
        return _validate_tier(default_tier)
    return infer_tier(source.source_type, source.author)


def _resolve_date(source: SourceIn) -> datetime:
    if source.date is None:
        return datetime.now(timezone.utc)
    if source.date.tzinfo is None or source.date.utcoffset() is None:
        # `SourceIn` normalises naive datetimes on the way in, so reaching
        # here means something bypassed the contract model.  A stored evidence
        # date with no offset is silently wrong by an unknown number of hours.
        raise ValueError(
            f"source.date for {source.title!r} is naive; an evidence timestamp "
            "must be timezone-aware (UTC) before it is stored"
        )
    return source.date


def _row_to_source(row: dict) -> Source:
    """Validate a DB row through the contract model."""
    data = dict(row)
    # `tier` is a Postgres enum stored as text.
    data["tier"] = int(data["tier"])
    return Source.model_validate(data)


def collect(source: SourceIn) -> Source:
    """Ingest `source` and return the stored row.

    Idempotent: a source whose content hashes to an existing row is returned
    unchanged rather than inserted again.
    """
    if not source.content or not source.content.strip():
        raise ValueError(
            f"source {source.title!r} has empty content; there is no evidence "
            "to ingest, so it cannot be stored"
        )

    tier = _resolve_tier(source)
    date = _resolve_date(source)
    sha256 = hashlib.sha256(source.content.encode("utf-8")).hexdigest()

    with db.tx() as cur:
        cur.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources "
            "WHERE content_sha256 = %s LIMIT 1",
            (sha256,),
        )
        existing = cur.fetchone()
        if existing is not None:
            return _row_to_source(existing)

        source_id = ids.next_id(cur, "SRC")
        cur.execute(
            "INSERT INTO sources (source_id, source_type, title, author, url, "
            "content, content_sha256, date, tier) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING " + _SOURCE_COLUMNS,
            (
                source_id,
                source.source_type,
                source.title,
                source.author,
                source.url,
                source.content,
                sha256,
                date,
                str(tier),  # enum column
            ),
        )
        return _row_to_source(cur.fetchone())


def get_source(source_id: str) -> Source | None:
    """Read one source.  None when it does not exist."""
    if not source_id:
        raise ValueError("source_id is required")
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    return _row_to_source(row) if row is not None else None


def list_sources(limit: int = 50) -> list[Source]:
    """Most recently ingested sources first."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    with db.tx() as cur:
        cur.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources "
            "ORDER BY ingested_at DESC, source_id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [_row_to_source(r) for r in rows]