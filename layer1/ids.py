"""Identifier generation.

IDs are sequential and human-readable (SRC_001, MECH_003, H012) because a
research database is read by people first and by Layer 2 second.  Sequence
allocation goes through Postgres so parallel writers cannot collide.
"""

from __future__ import annotations

import hashlib

#: Postgres sequence backing each ID prefix.
_SEQUENCES: dict[str, str] = {
    "SRC": "id_seq_source",
    "MECH": "id_seq_mechanism",
    "H": "id_seq_hypothesis",
    "EXP": "id_seq_experiment",
}

#: Hypotheses use H0001-style zero padding; the others use 3 digits.
_WIDTH: dict[str, int] = {"SRC": 3, "MECH": 3, "EXP": 3, "H": 4}


def format_id(prefix: str, value: int) -> str:
    width = _WIDTH[prefix]
    return f"{prefix}{value:0{width}d}"


def next_id(cur, prefix: str) -> str:
    """Allocate the next ID for `prefix` using the given cursor."""
    seq = _SEQUENCES[prefix]
    cur.execute(f"SELECT nextval('{seq}') AS v")
    return format_id(prefix, cur.fetchone()["v"])


def research_id(hypothesis_id: str, spec_sha256: str) -> str:
    """Deterministic provenance identity for one formalized hypothesis.

    Content-addressed on purpose: the same hypothesis formalized to the same
    spec yields the same research_id, so re-running the formalizer is
    idempotent and cannot silently fork provenance.
    """
    digest = hashlib.sha256(f"{hypothesis_id}:{spec_sha256}".encode()).hexdigest()
    return f"R_{digest[:16]}"