"""The closed research loop, end to end.

    source -> evidence -> mechanism -> hypothesis -> formalize
          -> critique -> integrity gate -> experiment queue
          -> RUN -> RESULT -> VERDICT -> negative knowledge

Runs against the deterministic stub backend, so it exercises the whole system
without a backtest engine and without network access.

IMPORTANT: every number this script prints from a run is FABRICATED. The stub
exists so the loop's plumbing can be tested; it has no predictive content, and
`is_real_engine` is False for it precisely so a reader can tell. Attaching a
real engine is a matter of implementing ExperimentAdapter -- see
layer2/rbi_adapter.py.

BOUNDARY: this script runs a research loop. It places no orders, holds no
positions, and contacts no exchange.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from layer1.config import get_settings
from layer1.contracts import SourceIn
from layer1.db import apply_schema
from layer1.pipeline import run_pipeline

from layer2 import analyst, rbi_adapter, runner
from layer2.rbi_adapter import RunOutcome
from layer2.stub_runner import StubAdapter


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def load_transcript() -> str:
    """Reuse the OFI transcript so the example needs no duplicate fixture."""
    text = (ROOT / "examples" / "ofi_pipeline.py").read_text(encoding="utf-8")
    match = re.search(r'TRANSCRIPT = """(.*?)"""', text, re.S)
    if not match:  # pragma: no cover - the fixture is part of this repo
        raise RuntimeError("could not read the OFI transcript from examples/ofi_pipeline.py")
    return match.group(1)


# A complete experimental protocol. Layer 1 owns no datasets, so it cannot
# invent these -- which is exactly why the integrity gate refuses to queue a
# hypothesis that lacks them.
PROTOCOL = {
    "temporal_protocol": {
        "train": {"start": "2021-01-01", "end": "2023-12-31"},
        "validation": {"start": "2024-01-01", "end": "2024-06-30"},
        "test": {"start": "2024-07-01", "end": "2025-12-31"},
        "horizon": "5 minutes",
        "split_type": "anchored",
    },
    "universe": {
        "assets": ["BTCUSDT perpetual"],
        "inclusion_rule": "top-20 by trailing 30d volume",
        "survivorship_policy": "point_in_time_index",
        "quote_currency": "USDT",
    },
    "execution": {
        "signal_time": "bar close",
        "execution_time": "next bar open",
        "price": "next bar VWAP",
        "latency_ms": 50.0,
        "timezone": "UTC",
    },
    "costs": {
        "commission_bps": 4.0,
        "spread_model": "book mid at decision time",
        "spread_bps": 1.2,
        "slippage_model": "half-spread + square-root impact",
        "slippage_bps": 0.8,
        "funding": "applied at 8h intervals",
    },
    "data": {
        "provider": "internal L2 archive",
        "dataset_version": "l2-2025-11-frozen",
        "timestamped_at": "2025-11-30T00:00:00Z",
        "timezone": "UTC",
    },
}


def main() -> int:
    settings = get_settings()
    deterministic = settings.llm_mode == "deterministic"
    if deterministic:
        from examples import canned

        canned.install()

    banner("THE CLOSED RESEARCH LOOP")
    print(f"llm mode : {settings.llm_mode} ({settings.llm_model_name})")
    print(f"backend  : stub  (is_real_engine=False -- numbers below are FABRICATED)")

    for name in ("schema.sql", "schema_evidence_ledger.sql", "schema_experiment.sql"):
        path = ROOT / "db" / name
        if path.exists():
            apply_schema(path.read_text(encoding="utf-8"))

    rbi_adapter.register(StubAdapter())

    # -- Layer 1 ---------------------------------------------------------
    banner("PART 1 -- LAYER 1: source -> ... -> experiment queue")
    run = run_pipeline(
        SourceIn(
            source_type="youtube",
            title="OFI and Microprice: Reading Intent From the Book",
            author="Moon Dev",
            content=load_transcript(),
            tier=4,
        ),
        max_mechanisms=1,
        hypotheses_per_mechanism=3,
        require_integrity=True,
    )
    print(json.dumps(run.summary(), indent=2))
    print("\nNote the result above: 0 queued. That is the integrity gate doing its")
    print("job -- every hypothesis was audited 'incomplete' because no experimental")
    print("protocol was supplied. An under-specified protocol produces irreproducible")
    print("results, the defect measured in 17 of 19 studies in the Agentic Trading")
    print("survey. Layer 1 owns no datasets, so it cannot invent a split, a universe")
    print("and a cost model.")

    if not run.hypothesis_ids:
        print("\nno hypotheses were generated; nothing to execute")
        return 1

    banner("PART 2 -- same source, WITH an experimental protocol")
    # A protocol is keyed by hypothesis_id, and that id does not exist until the
    # HYPOTHESIS stage has run -- so it has to be supplied as a callable rather
    # than a mapping built in advance. That is why run_pipeline accepts both.
    run = run_pipeline(
        SourceIn(
            source_type="youtube",
            title="OFI and Microprice: Reading Intent From the Book",
            author="Moon Dev",
            content=load_transcript(),
            tier=4,
        ),
        max_mechanisms=1,
        hypotheses_per_mechanism=3,
        require_integrity=True,
        protocols=lambda _hypothesis_id: PROTOCOL,
    )
    print(json.dumps(run.summary(), indent=2))

    if not run.queued:
        print(
            "\nStill nothing queued. Check the audits -- the critic or the\n"
            "integrity gate may have refused these particular hypotheses."
        )
        for entry in run.log:
            if entry["stage"] in {"critique", "integrity"}:
                print(f"  {entry}")
        print("\nSee docs/ARCHITECTURE.md for the full loop.")
        return 0

    job = run.queued[0]
    print(f"\nqueued {job.experiment_id} for hypothesis {job.hypothesis_id}")


    # -- Layer 2 ---------------------------------------------------------
    banner("PART 3 -- LAYER 2: run -> result -> verdict -> knowledge")
    l2run = runner.start_run(
        job.experiment_id,
        backend="stub",
        dataset=PROTOCOL["data"]["provider"],
        dataset_version=PROTOCOL["data"]["dataset_version"],
        universe=PROTOCOL["universe"],
        time_period={
            "train": PROTOCOL["temporal_protocol"]["train"],
            "test": PROTOCOL["temporal_protocol"]["test"],
        },
        timeframe="1m",
        cost_model=PROTOCOL["costs"],
        execution_model=PROTOCOL["execution"],
        random_seed=42,
    )
    print(f"run_id      : {l2run.run_id}")
    print(f"dataset     : {l2run.dataset_version}")

    outcome = RunOutcome(
        status="completed",
        outcome="supported",
        metrics={
            "gross_return": 0.42,
            "net_return": 0.11,
            "volatility": 0.18,
            "max_drawdown": -0.22,
            "sharpe": 1.3,
            "trade_count": 340,
            "turnover": 12.5,
            "hit_rate": 0.53,
            "mechanism_consistent": 1.0,
        },
        extra_metrics={"note": "stub run; numbers are fabricated"},
        stdout="stub run completed",
    )
    l2run = runner.complete_run(l2run.run_id, outcome)
    report = runner.run_report(l2run.run_id)
    print(f"\ntyped result (columns, not stdout):")
    print(f"  net_return            = {report['net_return']}")
    print(f"  sharpe                = {report['sharpe']}")
    print(f"  trade_count           = {report['trade_count']}")
    print(f"  mechanism_consistent  = {report['mechanism_consistent']}")
    print(f"  is_real_engine        = {report['is_real_engine']}")

    verdict = analyst.analyse(l2run.run_id)
    print(f"\nANALYST VERDICT: {verdict.kind.upper()}")
    print(f"  reasoning : {verdict.reasoning}")
    print(f"  mechanism : {verdict.mechanism_verdict}")

    knowledge = analyst.knowledge_for_next_generation()
    print(f"\nKNOWLEDGE available to the next hypothesis generation:")
    print(f"  recorded failures   : {len(knowledge.get('failures', []))}")
    print(f"  refuted mechanisms  : {len(knowledge.get('refuted_mechanisms', []))}")
    print(f"  contradictions      : {len(knowledge.get('contradictions', []))}")

    banner("THE LOOP IS CLOSED")
    print("A result became a verdict, and the verdict wrote knowledge back into")
    print("Layer 1. The next generation of hypotheses is generated in the light of")
    print("what has already been refuted.")
    print()
    print("Layer 2 is the stub backend. Attach a real engine by implementing")
    print("layer2.rbi_adapter.ExperimentAdapter -- Moon Dev's RBI engine is the")
    print("intended attachment. Until then, is_real_engine is False and no number")
    print("here is evidence of anything.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("L1_LLM_MODE", "deterministic")
    raise SystemExit(main())