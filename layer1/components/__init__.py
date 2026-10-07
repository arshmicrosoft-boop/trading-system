"""Layer 1 pipeline components.

Each module is one stage: collect -> extract -> generate -> formalize ->
critique -> queue.  They share `layer1.contracts` for types and
`layer1.db` for access, and never import each other's internals.
"""

from __future__ import annotations

__all__ = [
    "source_collector",
    "extractor",
    "generator",
    "formalizer",
    "critic",
    "queueing",
]