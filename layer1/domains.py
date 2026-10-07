"""Research domains -- explicit universes, not an indicator buffet.

Moon Dev's mistake worth avoiding: letting a model free-associate over every
TA indicator ever named.  That produces combinatorially many shallow
hypotheses that nobody can falsify.  A domain is a bounded universe with a
shared economic premise, so hypotheses generated inside it are comparable
to each other.

These are seed universes for prompt construction and tagging. They are data,
not enforcement -- the extractor may propose a variable outside them, but
it must then justify why it belongs to the mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Domain:
    key: str
    title: str
    premise: str
    variables: tuple[str, ...] = field(default_factory=tuple)


MICROSTRUCTURE = Domain(
    key="microstructure",
    title="Microstructure",
    premise=(
        "The limit order book's evolving state reflects the balance of "
        "unfulfilled intent, and that balance resolves before the next "
        "price move rather than after it."
    ),
    variables=(
        "multi_level_ofi",
        "microprice",
        "order_book_imbalance",
        "book_depth",
        "spread",
        "trade_imbalance",
        "aggressive_volume",
        "queue_position",
        "depth_resilience",
        "book_slope",
        "short_term_volatility",
    ),
)

FORCED_FLOWS = Domain(
    key="forced_flows",
    title="Forced flows",
    premise=(
        "Positions that must be closed regardless of price create order "
        "flow that is price-insensitive, and clustered forced flow is "
        "concentrated in time rather than spread uniformly."
    ),
    variables=(
        "liquidation_notional",
        "liquidation_cluster_density",
        "open_interest",
        "open_interest_change",
        "funding_rate",
        "funding_basis",
        "basis_spread",
        "deleveraging_pressure",
        "positioning_concentration",
    ),
)

RELATIVE_VALUE = Domain(
    key="relative_value",
    title="Relative value",
    premise=(
        "Prices of linked instruments are bound by an economic arbitrage "
        "relationship, so deviations from that bound mean-revert once "
        "liquidity providers re-establish the link."
    ),
    variables=(
        "cross_market_spread",
        "basis_spread",
        "lead_lag_correlation",
        "cointegration_spread",
        "liquidity_asymmetry",
        "basis_zscore",
        "cross_venue_price_gap",
    ),
)

PRICE_VOLUME = Domain(
    key="price_volume",
    title="Price/volume",
    premise=(
        "Order flow aggregates into persistent trends when participation is "
        "unbalanced, and reverses when temporary price impact decays faster "
        "than the flow persists."
    ),
    variables=(
        "realized_volatility",
        "volatility_regime",
        "volume_zscore",
        "trend_persistence",
        "breakout_range",
        "drawdown_from_high",
        "price_volume_divergence",
        "atr",
    ),
)

DOMAINS: tuple[Domain, ...] = (
    MICROSTRUCTURE,
    FORCED_FLOWS,
    RELATIVE_VALUE,
    PRICE_VOLUME,
)

DOMAINS_BY_KEY: dict[str, Domain] = {d.key: d for d in DOMAINS}

ALL_VARIABLES: tuple[str, ...] = tuple(
    sorted({v for d in DOMAINS for v in d.variables})
)


def describe_domains() -> str:
    """Prompt-ready rendering of the universes."""
    parts = []
    for d in DOMAINS:
        variables = ", ".join(d.variables)
        parts.append(f"### {d.title} ({d.key})\nPremise: {d.premise}\nVariables: {variables}")
    return "\n\n".join(parts)