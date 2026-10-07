"""Canned agent responses for offline pipeline runs.

The deterministic provider in `layer1.llm` reads its answer from the user
message under a ``__response__`` key. That makes it a *test seam*, not a
fallback: a caller who asks for deterministic mode must supply the exact
structured answer it should get.

This module supplies those answers for the OFI example so the pipeline can be
exercised end to end with no model and no network. Everything it returns is
marked as such downstream -- artifacts land with model_used='deterministic/v1'
so a reviewer can never mistake them for genuine model output.

These responses are plausible, hand-written research content. They are NOT
model output and must never be used as evidence that a prompt works.
"""

from __future__ import annotations

import json
from typing import Any

# --------------------------------------------------------------------------
# Stage 2 -- mechanism extraction
# --------------------------------------------------------------------------
MECHANISMS: dict[str, Any] = {
    "mechanisms": [
        {
            "mechanism_name": "order_flow_imbalance",
            "observation": (
                "Aggressive volume arriving at the bid consistently exceeds volume "
                "lifting the ask over short windows, and price tends to drift down "
                "during those windows."
            ),
            "mechanism_description": (
                "Order flow imbalance is a proxy for the imbalance in unfilled limit "
                "order intent. A persistent excess of market orders hitting one side "
                "means more resting interest is being consumed on that side than is "
                "replenished, and that deficit must be resolved either by passive "
                "sellers stepping in (price holds) or by price moving (imbalance "
                "resolves through the trade). The imbalance therefore carries "
                "information about near-term unfilled intent, not about momentum that "
                "has already been realized."
            ),
            "candidate_variables": [
                "multi_level_ofi",
                "order_book_imbalance",
                "aggressive_volume",
                "microprice",
                "short_term_volatility",
            ],
            "domain": "microstructure",
        },
        {
            "mechanism_name": "microprice_implied_edge",
            "observation": (
                "A depth-weighted price inside the spread sits closer to the bid when "
                "bid depth exceeds ask depth, and closer to the ask in the opposite case."
            ),
            "mechanism_description": (
                "Microprice expresses the same depth imbalance as order flow "
                "imbalance, but in price space rather than volume space. Because the "
                "two encode the same underlying quantity of unfilled intent, their "
                "disagreement indicates measurement noise from queue depletion or "
                "cancellations, while their agreement indicates a quantity large "
                "enough to survive that noise. Agreement is therefore expected to be "
                "the more robust signal than either measure alone."
            ),
            "candidate_variables": [
                "microprice",
                "multi_level_ofi",
                "book_depth",
                "spread",
            ],
            "domain": "microstructure",
        },
    ]
}

# --------------------------------------------------------------------------
# Stage 3 -- hypothesis generation
# --------------------------------------------------------------------------
HYPOTHESES: dict[str, Any] = {
    "hypotheses": [
        {
            "statement": (
                "Normalized multi-level order flow imbalance predicts the sign of the "
                "next short-horizon return."
            ),
            "expected_mechanism": (
                "Multi-level OFI estimates net unfilled intent across several book "
                "levels, which resolves into price movement once passive liquidity is "
                "exhausted."
            ),
            "candidate_variables": ["multi_level_ofi", "mid_price", "book_depth"],
            "null_hypothesis": (
                "Future returns are conditionally independent of current normalized "
                "multi-level OFI."
            ),
        },
        {
            "statement": (
                "Agreement between order flow imbalance and microprice direction "
                "carries more predictive information than either feature alone."
            ),
            "expected_mechanism": (
                "OFI and microprice encode the same quantity of unfilled intent in "
                "volume and price space respectively; agreement implies a true "
                "imbalance rather than measurement noise from queue depletion."
            ),
            "candidate_variables": ["multi_level_ofi", "microprice", "spread"],
            "null_hypothesis": (
                "Predictive content of the agreement condition equals the maximum "
                "predictive content of either feature considered individually."
            ),
        },
        {
            "statement": (
                "The first difference of order flow imbalance is more predictive than "
                "its absolute level."
            ),
            "expected_mechanism": (
                "A resting wall produces a permanently imbalanced book that carries "
                "little information, while rapid change in imbalance indicates "
                "intently arriving flow that is still unresolved."
            ),
            "candidate_variables": ["multi_level_ofi", "delta_multi_level_ofi"],
            "null_hypothesis": (
                "Absolute and first-difference order flow imbalance have equal "
                "predictive content for the next short-horizon return."
            ),
        },
        {
            "statement": (
                "The predictive content of order flow imbalance varies with the "
                "volatility and spread regime."
            ),
            "expected_mechanism": (
                "A given imbalance implies a price impact that scales with depth and "
                "falls with spread, so the same signal implies a larger and cheaper "
                "move in a tight, liquid book than in a wide, thin one."
            ),
            "candidate_variables": [
                "multi_level_ofi",
                "short_term_volatility",
                "spread",
                "book_depth",
            ],
            "null_hypothesis": (
                "Predictive content of order flow imbalance is constant across "
                "volatility and spread regimes."
            ),
        },
        {
            "statement": (
                "The magnitude of price movement predicted by order flow imbalance "
                "exceeds the round-trip transaction cost of acting on it."
            ),
            "expected_mechanism": (
                "Spread and fees are a fixed cost per unit of notional, so any signal "
                "with a constant expected move must lose money once cost is subtracted; "
                "only signals whose predicted move scales with size or liquidity clear "
                "the cost hurdle."
            ),
            "candidate_variables": [
                "multi_level_ofi",
                "spread",
                "book_depth",
                "realized_volatility",
            ],
            "null_hypothesis": (
                "Expected absolute price movement predicted by OFI is less than or "
                "equal to round-trip transaction cost, making the signal unprofitable "
                "at any observation threshold."
            ),
        },
    ]
}

# --------------------------------------------------------------------------
# Stage 4 -- rule formalization, keyed by hypothesis id.
# Only the fields the formalizer asks the model for; the assembler supplies
# hypothesis_id / source / status from the database.
# --------------------------------------------------------------------------
SPECS: dict[str, dict[str, Any]] = {
    "H0001": {
        "name": "MultiLevelOFI_Direction",
        "market": {"asset_class": "crypto", "universe": "perpetual_futures"},
        "features": ["multi_level_ofi", "mid_price", "book_depth"],
        "statement": (
            "Normalized multi-level order flow imbalance predicts the sign of the next "
            "short-horizon return."
        ),
        "condition": (
            "sign(zscore(multi_level_ofi, lookback)) == sign(fwd_return(horizon))"
        ),
        "horizon": "short_horizon_defined_by_experiment",
        "null": (
            "Future returns are conditionally independent of current normalized "
            "multi-level OFI."
        ),
        "expected_mechanism": "order_flow_imbalance",
    },
    "H0002": {
        "name": "OFI_Microprice_Agreement",
        "market": {"asset_class": "crypto", "universe": "perpetual_futures"},
        "features": ["multi_level_ofi", "microprice", "spread"],
        "statement": (
            "Agreement between order flow imbalance and microprice direction carries "
            "more predictive information than either feature alone."
        ),
        "condition": (
            "sign(multi_level_ofi) == sign(microprice_edge) and "
            "sign(microprice_edge) == sign(fwd_return(horizon))"
        ),
        "horizon": "short_horizon_defined_by_experiment",
        "null": (
            "Predictive content of the agreement condition equals the maximum "
            "predictive content of either feature considered individually."
        ),
        "expected_mechanism": "order_flow_imbalance_agreement",
    },
    "H0003": {
        "name": "OFI_Delta_vs_Level",
        "market": {"asset_class": "crypto", "universe": "perpetual_futures"},
        "features": ["multi_level_ofi", "delta_multi_level_ofi", "mid_price"],
        "statement": (
            "The first difference of order flow imbalance is more predictive than its "
            "absolute level."
        ),
        "condition": (
            "information_coefficient(delta_multi_level_ofi, fwd_return(horizon)) > "
            "information_coefficient(multi_level_ofi, fwd_return(horizon))"
        ),
        "horizon": "short_horizon_defined_by_experiment",
        "null": (
            "Absolute and first-difference order flow imbalance have equal predictive "
            "content for the next short-horizon return."
        ),
        "expected_mechanism": "changing_unfilled_intent",
    },
    "H0004": {
        "name": "OFI_Regime_Dependence",
        "market": {"asset_class": "crypto", "universe": "perpetual_futures"},
        "features": [
            "multi_level_ofi",
            "short_term_volatility",
            "spread",
            "book_depth",
        ],
        "statement": (
            "The predictive content of order flow imbalance varies with the volatility "
            "and spread regime."
        ),
        "condition": (
            "information_coefficient(multi_level_ofi, fwd_return(horizon)) is "
            "conditional on (short_term_volatility, spread) regime bucket"
        ),
        "horizon": "short_horizon_defined_by_experiment",
        "null": (
            "Predictive content of order flow imbalance is constant across volatility "
            "and spread regimes."
        ),
        "expected_mechanism": "regime_dependent_price_impact",
    },
    "H0005": {
        "name": "OFI_Exceeds_Costs",
        "market": {"asset_class": "crypto", "universe": "perpetual_futures"},
        "features": ["multi_level_ofi", "spread", "book_depth", "realized_volatility"],
        "statement": (
            "The magnitude of price movement predicted by order flow imbalance exceeds "
            "the round-trip transaction cost of acting on it."
        ),
        "condition": (
            "abs(predicted_move(multi_level_ofi)) > round_trip_cost(spread, fee_schedule)"
        ),
        "horizon": "short_horizon_defined_by_experiment",
        "null": (
            "Expected absolute price movement predicted by OFI is less than or equal "
            "to round-trip transaction cost, making the signal unprofitable at any "
            "observation threshold."
        ),
        "expected_mechanism": "cost_adjusted_information",
    },
}

# --------------------------------------------------------------------------
# Stage 5 -- hostile critique.
#
# Two of the five are failed on purpose: the pipeline should not pass
# everything. H0003 is refuted as not_falsifiable (an information-coefficient
# comparison across two features is a data question, not a single testable
# claim) and H0004 as not_measurable (a regime bucket with no definition is not
# a computable feature). That exercises the failure database.
# --------------------------------------------------------------------------
CRITIQUE_FAIL: dict[str, Any] = {
    "scores": {
        "mechanism_plausible": 2,
        "measurable": 2,
        "falsifiable": 0,
        "distinct": 1,
        "no_hidden_discretion": 2,
        "no_future_leakage": 2,
        "no_prior_failure_collision": 2,
    },
    "verdict": "FAIL",
    "failure_type": "not_falsifiable",
    "reason": (
        "The claim as stated compares the predictive content of two features "
        "against each other rather than asserting a measurable relationship with "
        "the market. Nothing observable at decision time distinguishes this from "
        "its null, because the comparison is itself the outcome being measured. "
        "Layer 2 would report the comparison rather than testing a hypothesis."
    ),
    "suggestions": [
        "State a single conditional relationship between one feature and forward "
        "returns, and let the level-versus-delta comparison be an experiment "
        "parameter rather than the claim.",
    ],
}

CRITIQUE_FAIL_REGIME: dict[str, Any] = {
    "scores": {
        "mechanism_plausible": 2,
        "measurable": 0,
        "falsifiable": 1,
        "distinct": 2,
        "no_hidden_discretion": 1,
        "no_future_leakage": 1,
        "no_prior_failure_collision": 2,
    },
    "verdict": "FAIL",
    "failure_type": "not_measurable",
    "reason": (
        "The rule conditions on a volatility and spread regime bucket that is "
        "never defined. A bucket requires a specific partitioning rule -- window, "
        "quantile cut, and inclusion criteria -- and none is given, so two "
        "implementations would compute different features and report contradictory "
        "results under the same hypothesis_id."
    ),
    "suggestions": [
        "Specify the regime partition explicitly: volatility percentile over a "
        "stated lookback, spread percentile over a stated lookback, and the bucket "
        "count.",
    ],
}

CRITIQUE_PASS: dict[str, Any] = {
    "scores": {
        "mechanism_plausible": 2,
        "measurable": 2,
        "falsifiable": 2,
        "distinct": 2,
        "no_hidden_discretion": 2,
        "no_future_leakage": 2,
        "no_prior_failure_collision": 2,
    },
    "verdict": "PASS",
    "failure_type": None,
    "reason": (
        "The mechanism is a documented microstructure effect with a clear economic "
        "story: net unfilled intent must eventually resolve into price. Every named "
        "feature is computable from standard L2 book data at decision time, the "
        "condition is an evaluable expression with no discretionary judgment, and "
        "the stated null is a concrete outcome that would refute it. No prior "
        "failure in the database covers this claim."
    ),
    "suggestions": [
        "Layer 2 should pin the horizon explicitly rather than leaving it to the "
        "experiment parameter space.",
    ],
}


def install() -> None:
    """Patch the LLM client so offline runs return the canned answers above.

    Dispatches on a marker the components embed in the user message. Kept out
    of `layer1` proper: this is an example/test fixture, and shipping it inside
    the engine would put canned research one stray import away from production.
    """
    from layer1 import llm

    def _fake_complete_json(self, system: str, user: str, **_: object):
        # `self` is required: this replaces a bound method, so the instance is
        # passed positionally. Leaving it out raises TypeError at call time.
        response = route(system, user)
        return response, "deterministic/v1"

    def route(system: str, user: str) -> Any:
        """Pick a canned answer.

        Dispatch is by call order within a single pipeline stage, not by
        matching prompt prose. Prompt text is expected to change (that is the
        point of prompt_version), and a router coupled to it breaks silently:
        the wrong canned answer gets returned and the demo still appears to
        work. A FIFO per stage is stable under rewording.
        """
        text = f"{system}\n{user}".lower()
        if _is_critic(text):
            stage = "critique"
        elif _is_extract(text):
            stage = "extract"
        elif _is_formalize(text):
            stage = "formalize"
        else:
            stage = "generate"

        index = _counters[stage]
        _counters[stage] = index + 1
        items = list(_BY_STAGE[stage])
        return items[index % len(items)]
    llm.LLMClient.complete_json = _fake_complete_json  # type: ignore[method-assign]


#: Canned answers grouped by pipeline stage. Keyed on the shape each component
#: asks for, so a prompt rewording does not change which answer is returned.
_BY_STAGE: dict[str, list[Any]] = {
    "extract": [MECHANISMS],
    "generate": [HYPOTHESES],
    "formalize": list(SPECS.values()),
    "critique": [
        CRITIQUE_PASS,
        CRITIQUE_FAIL,
        CRITIQUE_PASS,
        CRITIQUE_FAIL_REGIME,
        CRITIQUE_PASS,
    ],
}

#: Per-stage FIFO position, so repeated calls advance through the answers above.
_counters: dict[str, int] = {stage: 0 for stage in _BY_STAGE}


def _is_critic(text: str) -> bool:
    # The critic is the only stage that scores the seven named dimensions.
    return "no_future_leakage" in text


def _is_extract(text: str) -> bool:
    # The extractor is the only stage that asks for a list of mechanisms.
    return "mechanisms" in text and "hypothes" not in text


def _is_formalize(text: str) -> bool:
    # The formalizer is the only stage that asks for a signal condition.
    return "signal" in text and "condition" in text


def dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2)