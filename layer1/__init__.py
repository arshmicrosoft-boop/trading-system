"""MoonBot Layer 1 -- Research Engine.

Extracts falsifiable hypotheses from research evidence and queues them for
Layer 2 backtesting.  Layer 1 does not execute trades, connect to
exchanges, or decide what to trade.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]