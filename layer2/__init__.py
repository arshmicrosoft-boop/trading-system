"""MoonBot Layer 2 -- Execution.

Layer 2 runs the experiments Layer 1 queued, and records what came back as
structured rows: a typed metric set per run, the controlled-experiment
metadata that made the run comparable, and the artefacts needed to reproduce
it.  It closes the loop

    ... -> integrity gate -> experiment queue -> EXPERIMENT RESULT -> knowledge

Boundary, stated once so it never has to be restated:

* Layer 2 **does not decide what to trade.**  What to test is decided upstream
  by the Research Critic and the integrity gate; this layer's only power over a
  claim is to measure it and refuse to record a result it cannot vouch for.
* Layer 2 **holds no exchange credentials and reaches no exchange.**  There is
  no order, position, fill or balance concept anywhere in this package, and
  adding one would make every recorded number unfalsifiable -- a backtest that
  can trade is a backtest whose results depend on plumbing this repository
  does not have.
* Layer 2 **implements no engine.**  Execution happens behind the adapter port
  in :mod:`layer2.rbi_adapter`; Moon Dev RBI attaches there later.  The
  `execution_backends` table is the record of which engines exist and which of
  them are real, so an operator can see at a glance that the system has never
  run a real engine.

A result is only a result once it is typed: headline metrics are columns in
`experiment_results`, never text in a log line.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]