"""Stage 4 -- Rule Formalizer.

Turns a falsifiable hypothesis into the machine-readable document Layer 2
executes.

Two things are deliberately missing from that document.  The first is any
statement about expected profitability: ``RuleSpec`` forbids extra keys, so
an ``expected_return`` field cannot even be constructed, and this stage
re-checks the model's raw answer against the configured forbidden-key list
before it reaches the model at all.  The second is any room for
interpretation -- Layer 2 must be able to evaluate ``hypothesis.signal``
mechanically, so the signal condition is required to be an expression over
the declared features, not English that a human reads hopefully.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .. import llm
from ..config import get_settings
from ..contracts import (
    Hypothesis,
    HypothesisBlock,
    MarketBlock,
    RuleSpec,
    RuleSpecRecord,
    Signal,
)
from ..db import Jsonb, tx
from ..domains import ALL_VARIABLES
from . import extractor
from .generator import get_hypothesis

__all__ = ["formalize", "get_spec"]

#: The expression vocabulary offered to the model.  Kept deliberately
#: small: a formalizer that invents functions Layer 2 has never heard of
#: produces a spec that cannot run, and the failure surfaces days later
#: inside somebody else's experiment.
ALLOWED_FUNCTIONS: tuple[str, ...] = (
    "abs",
    "clip",
    "diff",
    "lag",
    "log",
    "mean",
    "rank",
    "sign",
    "std",
    "zscore",
)

#: Words that appear inside a condition but read nothing.  Without these a
#: conjunction silently becomes a feature Layer 2 has to compute.
_KEYWORDS = frozenset({"and", "or", "not", "in", "is", "if", "else", "true", "false", "none"})

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\(")
_OPERATOR = re.compile(r"==|!=|<=|>=|[<>+\-*/%]")
_KEY_NOISE = re.compile(r"[^0-9A-Za-z]+")

FORMALIZER_SYSTEM_PROMPT = """\
You are the Rule Formalizer of a quantitative research engine. You turn one
falsifiable hypothesis into a machine-readable rule specification.

You produce exactly these fields:
  name          a short snake_case identifier for the rule
  asset_class   the instrument class the rule applies to
  universe      the specific market or universe it applies to
  features      the concrete, computable variable names the rule reads
  condition     the signal condition, as an EXPRESSION a computer evaluates
  horizon       the time horizon the condition is evaluated on
  notes         optional short clarification of how to read the condition
  null          the observation that would refute this rule

The condition is code, not prose. Write it as an expression over the names
in `features`, for example:
  sign(multi_level_ofi) == sign(microprice)
  zscore(cross_venue_price_gap) < -2 and volume_zscore > 1.5
Use only these functions: {functions}
Do not write sentences, do not use placeholders, do not use English words
as operands.

Forbidden, in every field. Output containing these is discarded:
  * expected returns, profit targets, Sharpe targets, or any numeric target
  * position sizing, entries, exits, stops, or any instruction about what
    to trade, how much, or when

Reply with JSON only -- no prose, no commentary, no code fences -- in
exactly this shape:
{{"name": "...", "asset_class": "...", "universe": "...", "features": ["..."],
  "condition": "...", "horizon": "...", "notes": "...", "null": "..."}}
"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _looks_like_expression(condition: str) -> bool:
    """Reject prose so Layer 2 is never handed English to evaluate.

    Balanced parentheses plus a call or a comparator.  ``ofi > 0`` and
    ``sign(a) == sign(b)`` pass; "buy when the book is imbalanced" does
    not, because it contains neither.
    """
    if condition.count("(") != condition.count(")"):
        return False
    return bool(_CALL.search(condition) or _OPERATOR.search(condition))


def _feature_set(raw: Any, condition: str) -> list[str]:
    """Declared features unioned with the names the condition actually reads.

    Layer 2 binds the condition against exactly the feature list it is
    handed, so a condition mentioning an undeclared name would evaluate
    against nothing.  Reconciling here is cheaper and more honest than
    trusting the model's bookkeeping.  The extra names are sorted because
    the set iteration order would otherwise leak into the content hash and
    fork the spec version between identical runs.
    """
    declared = [raw] if isinstance(raw, str) else raw
    if not isinstance(declared, list):
        declared = []
    referenced = sorted(
        name
        for name in set(_IDENTIFIER.findall(condition))
        if name not in ALLOWED_FUNCTIONS and name.casefold() not in _KEYWORDS
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for name in (*(_text(item) for item in declared), *referenced):
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        ordered.append(name)
    return ordered


def _assert_no_profit_target(payload: Any, forbidden: frozenset[str]) -> None:
    """Fail closed on any profitability field anywhere in the answer.

    ``RuleSpec`` already refuses unknown top-level keys, but the model
    could still hide one under a block that legitimately exists, and the
    failure would only surface when Layer 2 read the document.  Matching
    ignores case and punctuation so ``Expected-Return`` is caught too.
    """
    if not forbidden:
        return
    blocked = {_KEY_NOISE.sub("", key).lower() for key in forbidden}
    stack: list[tuple[str, Any]] = [("$", payload)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and _KEY_NOISE.sub("", key).lower() in blocked:
                    raise llm.LLMError(
                        f"formalizer returned a profitability target at {path}.{key}; "
                        "Layer 1 does not encode expected returns"
                    )
                stack.append((f"{path}.{key}", value))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                stack.append((f"{path}[{index}]", value))


def _build_spec(payload: Any, hypothesis: Hypothesis, source_id: str) -> RuleSpec:
    if not isinstance(payload, dict):
        raise llm.LLMError("formalizer returned something that is not a JSON object")

    name = _text(payload.get("name"))
    condition = _text(payload.get("condition"))
    null = _text(payload.get("null"))
    for field, value in (("name", name), ("condition", condition), ("null", null)):
        if not value:
            raise llm.LLMError(f"formalizer returned no {field}")
    if not _looks_like_expression(condition):
        raise llm.LLMError(f"signal condition is prose, not an expression: {condition!r}")

    try:
        return RuleSpec(
            hypothesis_id=hypothesis.hypothesis_id,
            name=name.lower(),
            source=source_id,
            market=MarketBlock(
                asset_class=_text(payload.get("asset_class")) or "crypto",
                universe=_text(payload.get("universe")) or "unspecified",
            ),
            features=_feature_set(payload.get("features"), condition),
            hypothesis=HypothesisBlock(
                statement=hypothesis.statement,
                signal=Signal(
                    condition=condition,
                    horizon=_text(payload.get("horizon")) or "unspecified",
                    notes=_text(payload.get("notes")) or None,
                ),
                null=null,
            ),
            expected_mechanism=hypothesis.expected_mechanism,
            status="formalized",
        )
    except ValidationError as exc:
        raise llm.LLMError(f"formalizer produced an invalid rule spec: {exc}") from exc


def _record(row: dict[str, Any]) -> RuleSpecRecord:
    document = dict(row["yaml_spec"])
    return RuleSpecRecord(
        **document,
        version_id=row["version_id"],
        model_used=row["model_used"],
        prompt_version=row["prompt_version"],
        creation_timestamp=row["creation_timestamp"],
    )


_VERSION_COLUMNS = "version_id, yaml_spec, model_used, prompt_version, creation_timestamp"


def formalize(hypothesis_id: str) -> RuleSpecRecord:
    """Turn one hypothesis into a versioned, machine-readable rule spec.

    The refutation condition asked for here is what lands in
    ``HypothesisBlock.null``.  ``hypotheses`` has no column to carry the
    generator's version, so it is re-derived from the same statement at
    formalization time -- and this is the better place for it anyway,
    because it can be written against the concrete signal the spec
    actually declares.

    Re-running this never raises on an already formalized hypothesis: the
    ``UNIQUE (hypothesis_id, spec_sha256)`` constraint makes an identical
    re-derivation a no-op returning the stored row, and a spec that
    genuinely changed is appended as a new version, which is what that
    constraint exists for.  Blocking the second call instead would mean a
    prompt change could never be re-run without surgery on the database.
    """
    hypothesis = get_hypothesis(hypothesis_id)
    if hypothesis is None:
        raise KeyError(f"hypothesis {hypothesis_id} not found")
    mechanism = extractor.get_mechanism(hypothesis.mechanism_id)
    if mechanism is None:
        raise KeyError(f"mechanism {hypothesis.mechanism_id} not found")

    settings = get_settings()
    payload, model_name = llm.get_llm().complete_json(
        FORMALIZER_SYSTEM_PROMPT.format(functions=", ".join(ALLOWED_FUNCTIONS)),
        json.dumps(
            {
                "task": "formalize_hypothesis",
                "hypothesis": hypothesis.model_dump(mode="json"),
                "mechanism": mechanism.model_dump(mode="json"),
                "variable_pool": {
                    "from_hypothesis": hypothesis.candidate_variables,
                    "from_mechanism": mechanism.candidate_variables,
                    "global": list(ALL_VARIABLES),
                },
            },
            ensure_ascii=False,
        ),
    )
    _assert_no_profit_target(payload, settings.forbidden_spec_keys)
    spec = _build_spec(payload, hypothesis, mechanism.source_id)
    digest = spec.spec_sha256()

    with tx() as cur:
        cur.execute(
            f"""
            INSERT INTO hypothesis_versions
                (hypothesis_id, yaml_spec, spec_sha256, model_used, prompt_version)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (hypothesis_id, spec_sha256) DO NOTHING
            RETURNING {_VERSION_COLUMNS}
            """,
            (hypothesis_id, Jsonb(spec.model_dump(mode="json")), digest, model_name,
             settings.prompt_version),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"""
                SELECT {_VERSION_COLUMNS} FROM hypothesis_versions
                WHERE hypothesis_id = %s AND spec_sha256 = %s
                """,
                (hypothesis_id, digest),
            )
            row = cur.fetchone()
        cur.execute(
            "UPDATE hypotheses SET status = 'formalized', updated_at = now() "
            "WHERE hypothesis_id = %s",
            (hypothesis_id,),
        )
    if row is None:  # unreachable unless the conflict row vanished mid-transaction
        raise llm.LLMError(f"no stored version for hypothesis {hypothesis_id}")
    return _record(row)


def get_spec(hypothesis_id: str) -> RuleSpecRecord | None:
    """The most recent formalization of a hypothesis, if it has one."""
    with tx() as cur:
        cur.execute(
            f"""
            SELECT {_VERSION_COLUMNS} FROM hypothesis_versions
            WHERE hypothesis_id = %s
            ORDER BY version_id DESC
            LIMIT 1
            """,
            (hypothesis_id,),
        )
        row = cur.fetchone()
    return _record(row) if row else None