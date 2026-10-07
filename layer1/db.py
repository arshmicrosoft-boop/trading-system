"""Database access for Layer 1.

Thin over psycopg3's async pool: no ORM.  The schema is small and the
queries are explicit, so an ORM would add indirection without removing
any repetition.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import get_settings

__all__ = [
    "connect",
    "tx",
    "Jsonb",
    "apply_schema",
    "scalar_list",
]


def _dsn() -> str:
    return get_settings().database_url


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Single connection context manager."""
    conn = psycopg.connect(_dsn(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def tx() -> Iterator[psycopg.Cursor]:
    """Transaction-scoped cursor with dict rows."""
    with connect() as conn:
        with conn.cursor() as cur:
            yield cur


def apply_schema(schema_sql: str) -> None:
    """Execute the schema script.

    The script is authored to be idempotent (IF NOT EXISTS / OR REPLACE), so
    this runs safely on every boot against a fresh or a populated database.
    There is no text rewriting here on purpose: a rewrite layer over DDL is a
    second grammar that silently diverges from the first.
    """
    with connect() as conn:
        conn.execute(schema_sql)


def scalar_list(cur, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def as_jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def parse_jsonb(value: Any) -> Any:
    """psycopg returns JSONB as parsed Python objects already; passthrough."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value