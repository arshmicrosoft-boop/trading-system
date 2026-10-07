"""The canonical Layer 1 pipeline.

    sources -> evidence -> mechanisms -> hypotheses
            -> RuleSpec -> critique -> integrity gate -> experiment queue

This module exists because the stages were implemented independently and
nothing enforced their order. Two of them -- the Evidence Ledger and the
Integrity Gate -- were complete, tested modules that no caller ever invoked.
That is architecture drift in its most expensive form: a component that looks
like a guarantee and is not one.

So the ordering is defined here, once, and `run_pipeline` is the supported way
to move a source through the system.

One deliberate deviation from the nominal stage order: a hypothesis is
FORMALIZED before it is CRITIQUED. The critic is asked whether the signal is
computable and whether it leaks future information, and neither question is
answerable about a claim that has no rule yet. Formalization is what turns a
claim into something those questions can be asked of. The artifact ordering is
what the architecture guarantees, and it is enforced below rather than assumed.

What this module refuses to do:
  * skip a stage;
  * advance a hypothesis whose integrity audit did not pass;
  * queue a hypothesis the critic rejected.

What it deliberately does NOT do: backtest, price anything, or contact an
exchange. It stops at the queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping

from . import evidence, integrity, llm
from .components import critic, extractor, formalizer, generator, source_collector
from .contracts import ExperimentJob, MechanismRecord, RuleSpecRecord, Source, SourceIn

__all__ = [
    "Stage",
    "StageViolation",
    "PipelineRun",
    "ProtocolSource",
    "run_pipeline",
    "run_from_mechanism",
]


class Stage(StrEnum):
    """The canonical stages."""

    SOURCE = "source"
    EVIDENCE = "evidence"
    MECHANISM = "mechanism"
    HYPOTHESIS = "hypothesis"
    FORMALIZE = "formalize"
    CRITIQUE = "critique"
    INTEGRITY = "integrity"
    QUEUE = "queue"


#: The only legal stage order, enforced at runtime.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.SOURCE,
    Stage.EVIDENCE,
    Stage.MECHANISM,
    Stage.HYPOTHESIS,
    Stage.FORMALIZE,
    Stage.CRITIQUE,
    Stage.INTEGRITY,
    Stage.QUEUE,
)


class StageViolation(RuntimeError):
    """A pipeline transition the architecture forbids."""


@dataclass(slots=True)
class PipelineRun:
    """The trace of one source through the pipeline."""

    source: Source
    stage: Stage = Stage.SOURCE
    claim_ids: list[str] = field(default_factory=list)
    mechanisms: list[MechanismRecord] = field(default_factory=list)
    hypothesis_ids: list[str] = field(default_factory=list)
    critiques: dict[str, Any] = field(default_factory=dict)
    specs: dict[str, RuleSpecRecord] = field(default_factory=dict)
    audits: dict[str, str] = field(default_factory=dict)
    queued: list[ExperimentJob] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def reached(self) -> Stage:
        return self.stage

    def summary(self) -> dict[str, Any]:
        return {
            "source_id": self.source.source_id,
            "final_stage": self.stage.value,
            "claims": len(self.claim_ids),
            "mechanisms": len(self.mechanisms),
            "hypotheses": len(self.hypothesis_ids),
            "queued": len(self.queued),
            "rejected": len(self.rejected),
            "stopped_reason": self.stopped_reason,
        }


def _record(run: PipelineRun, stage: Stage, **detail: Any) -> None:
    run.log.append(
        {"at": datetime.now(timezone.utc).isoformat(), "stage": stage.value, **detail}
    )


def _require(run: PipelineRun, stage: Stage) -> None:
    """Assert the pipeline has reached `stage`."""
    if STAGE_ORDER.index(run.stage) < STAGE_ORDER.index(stage):
        raise StageViolation(
            f"cannot run {stage.value}: pipeline is at {run.stage.value}. "
            f"Order is {' -> '.join(s.value for s in STAGE_ORDER)}."
        )


def _advance(run: PipelineRun, stage: Stage) -> None:
    current = STAGE_ORDER.index(run.stage)
    target = STAGE_ORDER.index(stage)
    if target > current + 1:
        skipped = [s.value for s in STAGE_ORDER[current + 1 : target]]
        raise StageViolation(
            f"cannot jump {run.stage.value} -> {stage.value}; "
            f"{', '.join(skipped)} would be skipped"
        )
    run.stage = stage


# ---------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------

_EVIDENCE_SYSTEM_PROMPT = """\
You extract EVIDENCE from a single research source. You are not being asked
for trading ideas.

For each distinct assertion the source actually makes, return one entry:

  {
    "claims": [
      {
        "statement": "the assertion, in the source's own substance",
        "claim_type": "fact | author_claim | empirical_result",
        "section_ref": "page / section / timestamp, or null",
        "evidence_strength": "strong | moderate | weak",
        "agent_interpretation": "what you think this implies, or null"
      }
    ]
  }

CRITICAL -- claim_type describes the SOURCE, never you:
  fact             a verifiable, checkable statement
  author_claim     the source asserts it without presenting evidence
  empirical_result the source presents data supporting it

Do NOT emit "model_inference" here. Anything that is YOUR reading rather than
the source's statement belongs in agent_interpretation, and the system types it
as model_inference automatically. Labelling your own inference as an
empirical result is precisely the failure this distinction exists to prevent.

Do not invent claims the source does not make. An empty list is a valid and
useful answer: it means the source asserts nothing checkable.

Reply with a single JSON object and nothing else.
"""


def _stage_source(source_in: SourceIn) -> Source:
    return source_collector.collect(source_in)


def _stage_evidence(run: PipelineRun) -> list[str]:
    """Extract typed claims.

    This is where the Evidence Ledger stops being an island. Every claim is
    typed, and model output is recorded through `record_model_inference` --
    a function whose signature has no claim_type parameter at all, so no
    version of this call can present a model's reading as a source's finding.
    """
    payload, model = llm.get_llm().complete_json(
        _EVIDENCE_SYSTEM_PROMPT,
        json.dumps({"source": run.source.model_dump(mode="json")}),
    )
    ids: list[str] = []
    for raw in payload.get("claims", []) or []:
        if not isinstance(raw, dict):
            continue
        statement = (raw.get("statement") or "").strip()
        if not statement:
            continue
        claim = evidence.record_model_inference(
            run.source.source_id,
            statement,
            section_ref=raw.get("section_ref"),
            evidence_strength=raw.get("evidence_strength"),
            agent_interpretation=raw.get("agent_interpretation"),
        )
        ids.append(claim.claim_id)
    _record(run, Stage.EVIDENCE, claims=len(ids), model=model)
    return ids


def _stage_mechanisms(run: PipelineRun, max_mechanisms: int) -> list[MechanismRecord]:
    mechs = extractor.extract(run.source.source_id, max_mechanisms=max_mechanisms)
    _record(run, Stage.MECHANISM, count=len(mechs))
    return mechs


def _stage_hypotheses(run: PipelineRun, mechanism: MechanismRecord, count: int) -> list[str]:
    hyps = generator.generate(mechanism.mechanism_id, count=count)
    run.hypothesis_ids.extend(h.hypothesis_id for h in hyps)
    _record(
        run,
        Stage.HYPOTHESIS,
        mechanism=mechanism.mechanism_id,
        generated=[h.hypothesis_id for h in hyps],
    )
    return [h.hypothesis_id for h in hyps]


def _stage_formalize(run: PipelineRun, hypothesis_id: str) -> RuleSpecRecord:
    spec = formalizer.formalize(hypothesis_id)
    run.specs[hypothesis_id] = spec
    _record(run, Stage.FORMALIZE, hypothesis=hypothesis_id, rule=spec.name)
    return spec


def _stage_critique(run: PipelineRun, hypothesis_id: str) -> Any:
    """Critique, and RETAIN the verdict.

    `critic.critique` persists nothing; `apply_verdict` needs the object. Calling
    it twice would pay for the same LLM call twice and risk two different
    verdicts for one hypothesis. The verdict is held on the run for exactly
    this reason.
    """
    verdict = critic.critique(hypothesis_id)
    run.critiques[hypothesis_id] = verdict
    _record(
        run,
        Stage.CRITIQUE,
        hypothesis=hypothesis_id,
        verdict=verdict.verdict,
        scores=verdict.scores,
        reason=verdict.reason,
    )
    return verdict


def _stage_integrity(
    run: PipelineRun, hypothesis_id: str, protocol: dict[str, Any] | None
) -> bool:
    """Run the integrity gate. Returns whether the hypothesis may be queued.

    A hypothesis with no protocol supplied audits 'incomplete' and stops here.
    That is the honest outcome: Layer 1 does not own datasets, so it cannot
    invent a split, a universe and a cost model. Requiring them is the point --
    the Agentic Trading survey found 17 of 19 studies lacked one or more.
    """
    result = integrity.audit(hypothesis_id, protocol=protocol)
    run.audits[hypothesis_id] = result.status
    _record(
        run,
        Stage.INTEGRITY,
        hypothesis=hypothesis_id,
        status=result.status,
        missing=len(result.missing_protocol_fields),
        blocking=list(result.blocking_reasons),
    )
    return result.status == "pass" and integrity.gate_ready(hypothesis_id)


#: How a caller supplies per-hypothesis experimental protocols: either a
#: mapping keyed by hypothesis_id, or a callable that builds one on demand.
#: The callable form is required in practice -- see `_resolve_protocol`.
ProtocolSource = Mapping[str, dict[str, Any]] | Callable[[str], dict[str, Any] | None] | None


def _resolve_protocol(
    source: ProtocolSource, hypothesis_id: str
) -> dict[str, Any] | None:
    """Return the protocol for one hypothesis.

    Accepts either a mapping keyed by hypothesis_id or a callable taking the
    id. The callable form exists because of a real ordering problem: the id
    does not exist until the HYPOTHESIS stage has run, so a caller cannot
    build the mapping in advance. Without this, `run_pipeline` could never
    queue anything through its own entry point -- the integrity gate would
    audit every hypothesis as 'incomplete' by construction.
    """
    if source is None:
        return None
    if callable(source):
        return source(hypothesis_id)
    return source.get(hypothesis_id)


# ---------------------------------------------------------------------
# Per-hypothesis progression
# ---------------------------------------------------------------------


def _process_hypothesis(
    run: PipelineRun,
    hypothesis_id: str,
    protocol: ProtocolSource,
    require_integrity: bool,
) -> ExperimentJob | None:
    """Formalize -> critique -> integrity -> queue. Returns the job, or None.

    Stage advancement is per-hypothesis. After one hypothesis reaches QUEUE the
    run returns to HYPOTHESIS for the next, because the ordering constraint
    binds one hypothesis's lifecycle, not the run's global position.
    """
    run.stage = Stage.HYPOTHESIS

    _stage_formalize(run, hypothesis_id)
    _advance(run, Stage.FORMALIZE)

    verdict = _stage_critique(run, hypothesis_id)
    if verdict.verdict != "PASS":
        critic.apply_verdict(verdict)
        run.rejected.append(hypothesis_id)
        run.stage = Stage.HYPOTHESIS
        return None
    _advance(run, Stage.CRITIQUE)

    if require_integrity:
        if not _stage_integrity(run, hypothesis_id, _resolve_protocol(protocol, hypothesis_id)):
            run.rejected.append(hypothesis_id)
            run.stage = Stage.HYPOTHESIS
            return None
        _advance(run, Stage.INTEGRITY)

    job = critic.apply_verdict(verdict)
    if job is None:
        run.rejected.append(hypothesis_id)
        run.stage = Stage.HYPOTHESIS
        return None

    run.queued.append(job)
    _record(
        run,
        Stage.QUEUE,
        hypothesis=hypothesis_id,
        experiment=job.experiment_id,
        research_id=job.research_id,
    )
    _advance(run, Stage.QUEUE)
    return job


# ---------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------


def run_pipeline(
    source_in: SourceIn,
    *,
    protocols: ProtocolSource = None,
    max_mechanisms: int = 3,
    hypotheses_per_mechanism: int = 5,
    require_integrity: bool = True,
) -> PipelineRun:
    """Run one source through every stage, in order.

    Args:
        source_in: the evidence to ingest.
        protocols: per-hypothesis experimental protocol keyed by
            hypothesis_id. Layer 2 supplies these -- Layer 1 owns no datasets
            and cannot invent a split, a universe or a cost model. A
            hypothesis with no entry is audited 'incomplete' and, with
            `require_integrity` set, is not queued.
        require_integrity: when True (default) a hypothesis must pass the
            integrity gate before it can be queued.
    """
    run = PipelineRun(source=_stage_source(source_in))
    _record(run, Stage.SOURCE, source_id=run.source.source_id)

    run.claim_ids = _stage_evidence(run)
    _advance(run, Stage.EVIDENCE)

    mechanisms = _stage_mechanisms(run, max_mechanisms)
    if not mechanisms:
        run.stopped_reason = "no mechanism could be extracted from this source"
        return run
    run.mechanisms = mechanisms
    _advance(run, Stage.MECHANISM)

    for mechanism in mechanisms:
        for hypothesis_id in _stage_hypotheses(run, mechanism, hypotheses_per_mechanism):
            _process_hypothesis(
                run,
                hypothesis_id,
                protocols,
                require_integrity,
            )

    run.stopped_reason = (
        f"completed: {len(run.queued)} queued, {len(run.rejected)} rejected"
    )
    return run


def run_from_mechanism(
    mechanism_id: str,
    *,
    protocols: ProtocolSource = None,
    count: int = 5,
    require_integrity: bool = True,
) -> PipelineRun:
    """Resume the pipeline from an existing mechanism.

    SOURCE and EVIDENCE are already satisfied for this lineage, and pretending
    otherwise would re-charge the LLM for work already done.
    """
    mechanism = extractor.get_mechanism(mechanism_id)
    if mechanism is None:
        raise ValueError(f"no mechanism with id {mechanism_id!r}")

    source = source_collector.get_source(mechanism.source_id)
    if source is None:
        raise ValueError(
            f"mechanism {mechanism_id!r} references source {mechanism.source_id!r}, "
            "which does not exist -- the provenance chain is broken"
        )

    run = PipelineRun(source=source, stage=Stage.MECHANISM, mechanisms=[mechanism])
    for hypothesis_id in _stage_hypotheses(run, mechanism, count):
        _process_hypothesis(
            run,
            hypothesis_id,
            protocols,
            require_integrity,
        )

    run.stopped_reason = (
        f"resumed from {mechanism_id}: {len(run.queued)} queued, "
        f"{len(run.rejected)} rejected"
    )
    return run