"""HTTP surface for the MoonBot Layer 1 research engine.

This module is a transport adapter and nothing more: every route delegates to
a component in :mod:`layer1.components` and returns its result unchanged.  No
business rule, no prompt and no persistence logic lives here -- if one ever
did, it would be untestable from the pipeline path.

BOUNDARY.  Layer 1 is research only.  It reads evidence, extracts mechanisms,
generates falsifiable hypotheses, formalizes them into rule specs, critiques
them, and queues survivors for Layer 2.  There is no trading, no exchange
connectivity, no market-data feed, no order or position concept and no
backtest in this file or in anything it calls.  A request that would need any
of those does not belong on this server.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import llm
from .components.critic import apply_verdict, critique
from .components.extractor import extract, list_mechanisms
from .components.formalizer import formalize
from .components.generator import generate, get_hypothesis, list_hypotheses
from .components.queueing import list_failures, list_queue
from .components.source_collector import collect, get_source, list_sources
from .contracts import (
    Critique,
    ExperimentJob,
    FailureRecord,
    Hypothesis,
    MechanismRecord,
    RuleSpecRecord,
    Source,
    SourceIn,
)
from .db import apply_schema

__all__ = ["app", "router"]

logger = logging.getLogger("layer1.api")

#: The schema lives next to the package rather than inside it, so it stays
#: plain reviewable SQL.  It is resolved once at import time: a boot that
#: picked a different file depending on the working directory is exactly the
#: drift this engine cannot afford.
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"

#: How far back to look for the failure row a FAIL verdict just wrote.  The
#: verdict route does not receive that row, so the newest match is the only
#: handle the caller has; a bounded window keeps that lookup honest.
_FAILURE_LOOKBACK = 200


# ---------------------------------------------------------------------
# Request DTOs
#
# Request bodies are modelled here rather than in ``contracts.py`` because
# they are an HTTP concern: they carry transport-level bounds.  They only
# narrow the component signatures (limits, non-empty ids), never widen them,
# so no request can reach a component with arguments its own signature did
# not sanction.
# ---------------------------------------------------------------------


class ExtractRequest(BaseModel):
    """Body of ``POST /research/extract``."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    max_mechanisms: int = Field(default=8, ge=1, le=32)


class GenerateRequest(BaseModel):
    """Body of ``POST /hypotheses/generate``."""

    model_config = ConfigDict(extra="forbid")

    mechanism_id: str = Field(min_length=1)
    count: int = Field(default=5, ge=1, le=50)


class CritiqueResponse(BaseModel):
    """Body of ``POST /hypotheses/{id}/critique``.

    Verdict and consequence travel together on purpose: a caller seeing FAIL
    must be able to tell that the hypothesis was *recorded as a failure*
    rather than silently dropped, and a caller seeing PASS must hold the job
    id it must come back for.
    """

    model_config = ConfigDict(extra="forbid")

    critique: Critique
    queued_experiment_id: str | None = None
    failure_id: int | None = None


# ---------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Apply the schema on boot, then serve.

    The script is idempotent, so applying it every start makes a fresh
    database and a populated one indistinguishable to the API.  A missing
    file is logged rather than fatal: this database is often provisioned out
    of band (``db/schema.sql`` applied by hand or by a container
    entrypoint), and refusing to boot against an already-correct schema
    would be a worse failure than a warning.
    """
    if SCHEMA_PATH.exists():
        apply_schema(SCHEMA_PATH.read_text(encoding="utf-8"))
        logger.info("applied schema from %s", SCHEMA_PATH)
    else:
        logger.warning(
            "schema file %s not found; assuming the database is provisioned", SCHEMA_PATH
        )
    yield


app = FastAPI(
    title="MoonBot Layer 1 Research Engine",
    version="1.0.0",
    description=(
        "Layer 1 is a RESEARCH ENGINE ONLY. It ingests evidence, extracts "
        "mechanisms, generates falsifiable hypotheses, formalizes them into "
        "machine-readable rule specs, criticizes them, and queues survivors "
        "for Layer 2.\n\n"
        "It does NOT trade, place or manage orders, hold positions, connect "
        "to any exchange or broker, fetch market data, or run backtests. "
        "Layer 1 holds no opinion about what is profitable -- there is no "
        "expected-return field anywhere in its contracts. Deciding whether a "
        "hypothesis makes money is Layer 2's work, and Layer 2's only input "
        "from this service is GET /experiments/queue."
    ),
    lifespan=lifespan,
)

# A local Layer 2 or a notebook calls this from a different origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()
app.include_router(router)


# ---------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------

_HIDDEN_DETAIL = "internal server error"


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Keep a malformed request a 422.

    ``RequestValidationError`` subclasses ``ValueError``, so the handler
    below would otherwise swallow it and report a 400 for the client's own
    bad JSON.  Only location, message and type are echoed: the offending
    input may contain submitted research text, and a validation error is not
    a reason to reflect it back.
    """
    problems = [
        {
            "loc": list(err.get("loc", ())),
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,  # literal: starlette's named constant for this is deprecated
        content={"detail": problems},
    )


@app.exception_handler(ValueError)
async def _on_value_error(request: Request, exc: ValueError) -> JSONResponse:
    """A component rejected the request's content -- a client-side problem."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(llm.LLMError)
async def _on_llm_error(request: Request, exc: llm.LLMError) -> JSONResponse:
    """The upstream model was unreachable or returned unusable output.

    502 rather than 500: the request was well formed and Layer 1 is what
    could not satisfy it.  The detail is operator-facing and describes a
    provider fault, never a client one.
    """
    logger.warning("LLM failure on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": f"upstream model failure: {exc}"},
    )


@app.exception_handler(KeyError)
async def _on_key_error(request: Request, exc: KeyError) -> JSONResponse:
    """A lookup named something that does not exist."""
    logger.info("KeyError on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": f"not found: {exc}"},
    )


@app.exception_handler(Exception)
async def _on_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, return nothing useful to the client.

    The traceback goes to the server log and never to the response -- a stack
    trace carries file paths, SQL fragments and possibly submitted content,
    none of which a client should receive.  The client gets a fixed string
    and can quote the path when reporting the problem.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": _HIDDEN_DETAIL},
    )


def _latest_failure_id(hypothesis_id: str, failure_type: str) -> int | None:
    """Newest recorded failure for this hypothesis, or None.

    ``apply_verdict`` returns only the queued job, so on a FAIL the row it
    just wrote is unreachable through that return value.  Reading it back
    here keeps the component contract narrow instead of widening it for one
    caller's convenience.
    """
    matches = [
        failure
        for failure in list_failures(failure_type=failure_type, limit=_FAILURE_LOOKBACK)
        if failure.hypothesis_id == hypothesis_id
    ]
    if not matches:
        return None
    return max(matches, key=lambda f: f.created_at).failure_id


# ---------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------


@router.post(
    "/sources",
    response_model=Source,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a research source",
)
async def post_source(body: SourceIn) -> Source:
    """Store a piece of research evidence (note, article, PDF text, code
    repository, transcript) and return the immutable stored record with its
    assigned ``source_id`` and content hash.

    This is the entry point of the whole engine: nothing downstream exists
    until evidence has been ingested.
    """
    return collect(body)


@router.get("/sources", response_model=list[Source], summary="List stored sources")
async def get_sources(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[Source]:
    """Return the most recently ingested sources, newest first."""
    return list_sources(limit=limit)


@router.get(
    "/sources/{source_id}",
    response_model=Source,
    summary="Fetch one source",
    responses={404: {"description": "No source with that id"}},
)
async def get_source_by_id(source_id: str) -> Source:
    """Return one stored source by id, or 404 if it was never ingested."""
    source = get_source(source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no source with id {source_id!r}",
        )
    return source


# ---------------------------------------------------------------------
# Mechanisms
# ---------------------------------------------------------------------


@router.post(
    "/research/extract",
    response_model=list[MechanismRecord],
    summary="Extract economic mechanisms from a source",
)
async def post_extract(body: ExtractRequest) -> list[MechanismRecord]:
    """Read one ingested source and return the economic mechanisms it states
    or implies -- the causal stories that could later become falsifiable
    claims.  A mechanism is not a signal and carries no profitability
    expectation.
    """
    return extract(body.source_id, max_mechanisms=body.max_mechanisms)


@router.get("/mechanisms", response_model=list[MechanismRecord], summary="List mechanisms")
async def get_mechanisms(
    source_id: Annotated[str | None, Query(min_length=1)] = None,
) -> list[MechanismRecord]:
    """Return stored mechanisms, optionally narrowed to a single source."""
    return list_mechanisms(source_id=source_id)


# ---------------------------------------------------------------------
# Hypotheses
# ---------------------------------------------------------------------


@router.post(
    "/hypotheses/generate",
    response_model=list[Hypothesis],
    summary="Generate falsifiable hypotheses from a mechanism",
)
async def post_generate(body: GenerateRequest) -> list[Hypothesis]:
    """Turn one mechanism into candidate hypotheses.

    Each states what must be observed and carries the refuting observation,
    so it can be killed by evidence instead of defended by argument.  Nothing
    here asserts that a hypothesis is true, tradable or profitable.
    """
    return generate(body.mechanism_id, count=body.count)


@router.get("/hypotheses", response_model=list[Hypothesis], summary="List hypotheses")
async def get_hypotheses(
    status_filter: Annotated[str | None, Query(alias="status", min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[Hypothesis]:
    """Return stored hypotheses, optionally filtered by lifecycle status
    (``proposed``, ``critiqued``, ``rejected``, ``formalized``, ``queued``).

    The query parameter is spelled ``status`` for the caller; the handler
    renames it because ``status`` is the name of a Starlette response object
    in this module's namespace.
    """
    return list_hypotheses(status=status_filter, limit=limit)


@router.get(
    "/hypotheses/{hypothesis_id}",
    response_model=Hypothesis,
    summary="Fetch one hypothesis",
    responses={404: {"description": "No hypothesis with that id"}},
)
async def get_hypothesis_by_id(hypothesis_id: str) -> Hypothesis:
    """Return one hypothesis by id, or 404 if it does not exist."""
    hypothesis = get_hypothesis(hypothesis_id)
    if hypothesis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no hypothesis with id {hypothesis_id!r}",
        )
    return hypothesis


@router.post(
    "/hypotheses/{hypothesis_id}/formalize",
    response_model=RuleSpecRecord,
    summary="Formalize a hypothesis into a rule spec",
    responses={404: {"description": "No hypothesis with that id"}},
)
async def post_formalize(hypothesis_id: str) -> RuleSpecRecord:
    """Render one hypothesis into the machine-readable rule spec Layer 2
    consumes: market block, feature set, signal condition, horizon, and the
    explicit null that would refute it.

    The spec has no field for an expected return, so a profitability target
    is unrepresentable rather than merely discouraged.  Formalizing an
    already-formalized hypothesis produces a new immutable version.
    """
    return formalize(hypothesis_id)


@router.post(
    "/hypotheses/{hypothesis_id}/critique",
    response_model=CritiqueResponse,
    summary="Critique a formalized hypothesis and act on the verdict",
    responses={404: {"description": "No hypothesis with that id"}},
)
async def post_critique(hypothesis_id: str) -> CritiqueResponse:
    """Run the Research Critic against a hypothesis, then immediately apply
    the verdict.  The hypothesis must already be formalized: the critic reads
    the rule spec, not the prose.

    PASS enqueues an experiment for Layer 2 and returns its id in
    ``queued_experiment_id``.  FAIL records a typed failure -- naming the
    dimension that failed -- and returns its id in ``failure_id``.  Exactly
    one is set, so a caller never has to guess whether a rejection was
    recorded or lost.
    """
    verdict = critique(hypothesis_id)
    job = apply_verdict(verdict)
    return CritiqueResponse(
        critique=verdict,
        queued_experiment_id=job.experiment_id if job is not None else None,
        failure_id=(
            None
            if job is not None or verdict.failure_type is None
            else _latest_failure_id(hypothesis_id, verdict.failure_type)
        ),
    )


# ---------------------------------------------------------------------
# Queue and failures
# ---------------------------------------------------------------------


@router.get(
    "/experiments/queue",
    response_model=list[ExperimentJob],
    summary="Layer 2 consumption endpoint: the formalized experiment queue",
)
async def get_queue(
    status_filter: Annotated[str | None, Query(alias="status", min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[ExperimentJob]:
    """Return the experiments Layer 1 has admitted, optionally filtered by
    status (``queued``, ``claimed``, ``completed``).

    This is the ONLY handoff point to Layer 2, and every row is
    self-contained: provenance is joined in so Layer 2 needs no second call.
    For each job Layer 2 must:

      * treat ``experiment_id`` as the idempotency key and never run one
        twice, even if it re-reads the queue;
      * build features from ``feature_set`` only, against the snapshot named
        by ``data_version``, and refuse to proceed if that version is
        unavailable rather than substituting a newer one;
      * execute ``rule_spec`` verbatim.  It is an immutable versioned
        contract (``rule_version`` / ``version_id``) already sealed with
        ``research_id``, so Layer 2 does not reinterpret, retune or extend
        it;
      * test the hypothesis's ``null`` alongside the signal -- a job that
        measures only the promising side has measured nothing;
      * record the terminal outcome and move the row through ``claimed`` to
        ``completed``.  Layer 1 does not decide what any result means.

    Layer 2 -- not this service -- owns market data, simulation and every
    trading concern.  Nothing here reports a performance figure, because
    Layer 1 has none to report.
    """
    return list_queue(status=status_filter, limit=limit)


@router.get("/failures", response_model=list[FailureRecord], summary="List recorded failures")
async def get_failures(
    failure_type: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[FailureRecord]:
    """Return hypotheses that were rejected, optionally filtered by
    ``failure_type``.

    This is the engine's negative memory: a typed record of *why* a
    hypothesis died, so a later generation does not re-propose it.  Rows are
    append-only.
    """
    return list_failures(failure_type=failure_type, limit=limit)


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------


@router.get("/health", summary="Liveness, and the boundary restated")
async def get_health() -> dict[str, Any]:
    """Report that the service is up, and restate what it is not.

    ``trading: false`` is a standing declaration, not a status flag: no code
    path in this service can place an order.
    """
    return {"status": "ok", "layer": "layer1", "trading": False}