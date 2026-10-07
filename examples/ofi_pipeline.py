"""End-to-end Layer 1 pipeline: YouTube transcript -> experiment queue.

This is the V1 definition of done, executed end to end:

    transcript -> source stored -> OFI mechanism extracted
                -> 5 hypotheses generated -> formalized
                -> critiqued (hostile) -> passed ones queued for Layer 2

Run with a real model (slow on CPU, honest results):

    L1_LLM_MODE=ollama python examples/ofi_pipeline.py

Run offline with canned agent responses (fast, deterministic, and the
resulting artifacts are stamped ``deterministic/v1``):

    L1_LLM_MODE=deterministic python examples/ofi_pipeline.py

BOUNDARY: this script stores research and queues experiments. It does not
backtest, price anything, or trade.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layer1 import llm
from layer1.components import critic, extractor, formalizer, generator, source_collector
from layer1.config import get_settings
from layer1.contracts import SourceIn
from layer1.db import apply_schema

# ---------------------------------------------------------------------------
# Stage 0 -- the evidence
#
# Condensed from Moon Dev's OFI / microprice work. The point of the example is
# the pipeline, not the transcript, so this is representative rather than
# verbatim.
# ---------------------------------------------------------------------------
TRANSCRIPT = """
Today I want to talk about order flow imbalance and why the limit order book
tells you something about the next few seconds of price movement before that
movement actually happens.

Order flow imbalance measures the difference between the volume that arrives
at the best bid and the volume that arrives at the best ask over a short window.
The intuition is straightforward. When market orders are hitting the bid harder
than they are lifting the ask, there is more unfilled sell intent than buy
intent at the top of book, and that imbalance has to be resolved somehow. It
either gets replenished by passive limit sellers stepping in, or price moves
down. That is the mechanism: an imbalance in the arrival rate of aggressive
orders reflects an imbalance in unfilled intent.

I want to extend this beyond the top of book. A multi-level order flow
imbalance aggregates the imbalance across several levels of depth, not just the
touch. This is more robust because a single level can be spoofed, but the
pattern across five levels is much harder to fake.

Then there is microprice. Microprice weights the best bid and ask prices by the
depth resting at each side, giving a weighted average price that sits inside
the spread rather than at the midpoint. If the bid side is heavy, microprice
biases toward the bid. That is the same imbalance idea expressed in price space
instead of volume space, which is why I think the two should agree, and why
agreement between them should carry more information than either alone.

One thing people get wrong: the absolute level of order flow imbalance is far
less useful than the change in it. A book that is permanently imbalanced because
of a resting wall tells you very little. A book that is rapidly becoming imbalanced
tells you a lot about the next few seconds.

I also think the effect is not uniform. When volatility is high and the spread
is wide, you are crossing more cost for the same predicted move, and the
relationship gets weaker. Order flow imbalance works best in quiet, tight books
where the predicted move is large relative to the spread.

Finally, and this is the part people skip: none of this matters unless the
predicted move is bigger than the cost of getting into and out of the position.
Any signal that predicts a move smaller than the spread plus fees is not a
signal, it is a rounding error.
"""

TITLE = "Order Flow Imbalance and Microprice: Reading Intent From the Book"
AUTHOR = "Moon Dev"


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def main() -> int:
    settings = get_settings()
    deterministic = settings.llm_mode == "deterministic"

    if deterministic:
        # The deterministic provider reads its canned answer out of the user
        # message, so the components need it wired in. In a real run this is
        # unnecessary -- the LLM is asked normally.
        from examples import canned

        canned.install()

    banner("Layer 1 -- Research Engine")
    print(f"llm mode : {settings.llm_mode}")
    print(f"model    : {settings.llm_model_name}")
    print(f"database : {settings.database_url.rstrip('/moonbot')}/moonbot")
    if deterministic:
        print("\nNOTE: deterministic mode -- artifacts will be stamped "
              "'deterministic/v1'.")

    schema = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
    if schema.exists():
        apply_schema(schema.read_text(encoding="utf-8"))

    # -- Stage 1: source collector -----------------------------------------
    banner("Stage 1 -- Source Collector")
    source = source_collector.collect(
        SourceIn(
            source_type="youtube",
            title=TITLE,
            author=AUTHOR,
            url="https://youtube.com/@moondev",
            content=TRANSCRIPT,
            tier=4,  # YouTube claim: real evidence, weak provenance weight.
        )
    )
    print(f"source_id       : {source.source_id}")
    print(f"tier            : {source.tier} (YouTube)")
    print(f"content sha256  : {source.content_sha256[:24]}...")
    print(f"word count      : {source.word_count}")
    print("stored immutable (a DB trigger rejects any UPDATE)")

    # -- Stage 2: research extractor ---------------------------------------
    banner("Stage 2 -- Research Extractor  (mechanisms, not trades)")
    mechanisms = extractor.extract(source.source_id, max_mechanisms=3)
    for m in mechanisms:
        print(f"\n  {m.mechanism_id}  [{m.domain}]  {m.mechanism_name}")
        print(f"    observation : {m.observation}")
        print(f"    mechanism   : {m.mechanism_description}")
        print(f"    variables   : {', '.join(m.candidate_variables)}")
    if not mechanisms:
        print("no mechanisms extracted -- aborting")
        return 1

    # -- Stage 3: hypothesis generator -------------------------------------
    banner("Stage 3 -- Hypothesis Generator  (5 falsifiable hypotheses)")
    total_queued = 0
    total_rejected = 0
    total_errors = 0

    for mechanism in mechanisms:
        print(f"\n--- {mechanism.mechanism_id}: {mechanism.mechanism_name}")
        hypotheses = generator.generate(mechanism.mechanism_id, count=5)
        for h in hypotheses:
            print(f"  {h.hypothesis_id}  {h.statement}")
            print(f"        mechanism: {h.expected_mechanism}")

            # A single unformalizable hypothesis must not abort the batch.
            # Small local models truncate output, and a truncated signal
            # condition is a real, reportable defect rather than a reason to
            # discard the other four. The rejection is still visible -- this
            # is a demo, and hiding a failure would misrepresent the system.
            try:
                spec = formalizer.formalize(h.hypothesis_id)
            except llm.LLMError as exc:
                total_errors += 1
                print(f"        FORMALIZE FAILED: {exc}")
                print(f"        (skipped: {h.hypothesis_id} never reached the critic)")
                continue

            print(f"        rule     : {spec.name}")
            print(f"        condition: {spec.hypothesis.signal.condition}")
            print(f"        null     : {spec.hypothesis.null}")

            # -- Stage 5: research critic ----------------------------------
            # Critique requires a formalized spec: you cannot judge
            # measurability or future-leakage of an informalized claim.
            try:
                verdict = critic.critique(h.hypothesis_id)
            except llm.LLMError as exc:
                total_errors += 1
                print(f"        CRITIC FAILED: {exc}")
                continue

            print(f"        CRITIC   : {verdict.verdict}")
            print(f"        scores   : {json.dumps(verdict.scores)}")
            print(f"        reason   : {verdict.reason}")
            if verdict.failure_type:
                print(f"        failure  : {verdict.failure_type}")

            job = critic.apply_verdict(verdict)
            if job is not None:
                total_queued += 1
                print(f"        QUEUED   : {job.experiment_id} "
                      f"(research_id={job.research_id})")
            else:
                total_rejected += 1

    # -- Summary ------------------------------------------------------------
    banner("Summary -- the Layer 1 / Layer 2 boundary")
    from layer1.components import queueing

    queue = queueing.list_queue()
    failures = queueing.list_failures(limit=100)

    print(f"queued for Layer 2 : {total_queued}")
    print(f"rejected by critic : {total_rejected}")
    if total_errors:
        print(f"agent errors       : {total_errors} (unformalizable/uncritable; see above)")
    print(f"failure database   : {len(failures)} entries")

    if queue:
        print("\nLayer 2 will consume these via GET /experiments/queue:")
        for job in queue[:5]:
            print(f"  {job.experiment_id}  {job.hypothesis_id}  "
                  f"rule={job.rule_version}  features={','.join(job.feature_set)}")
            print(f"      {job.hypothesis_statement}")

    print("\nLayer 1 stops here. No backtest was run, no price was fetched,")
    print("no exchange was contacted. See docs/LAYER2.md for consumption.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("L1_LLM_MODE", "deterministic")
    raise SystemExit(main())