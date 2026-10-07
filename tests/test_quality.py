"""Tests for `layer1.quality`: research-quality scoring and lineage.

Two things are worth stating about how these tests are written.

The band boundaries are tested against the *database*, not against a helper
function.  20/21, 30/31 and 35/36 are off-by-one traps, and the arithmetic
that produces them is the thing most likely to be wrong -- the column check
`total_score = sum(eight dimensions)` rejects a mis-summed row, but it does so
as a constraint violation at insert time, which is a confusing way to discover
a bad sum.  So the boundary cases are actually written to `hypothesis_quality`
here, which proves the computed total and the band agree with the constraint
rather than merely agreeing with themselves.  One test deliberately submits a
mis-summed row to confirm the constraint is actually live in the deployed
database and not merely present in the schema file.

The cycle tests run the walk on a daemon thread under a deadline.  A recursive
walk without a visited set does not fail, it hangs -- there is no exception to
assert and no value to compare, and the suite would sit there until it was
killed.  A deadline turns "hangs forever" into a test failure.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

import psycopg
import pytest

from layer1 import db, quality
from layer1.quality import (
    PRIORITY_BANDS,
    QUALITY_DIMENSIONS,
    DimensionError,
    MutationTypeError,
    QualityError,
)


def _rid(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex[:12]}"


def _scores_for(total: int) -> dict[str, int]:
    """A legal eight-dimension block summing to exactly `total`."""
    assert 0 <= total <= quality.MAX_TOTAL
    remaining = total
    block: dict[str, int] = {}
    for dim in QUALITY_DIMENSIONS:
        value = min(quality.MAX_DIMENSION, remaining)
        block[dim] = value
        remaining -= value
    assert remaining == 0
    return block


@dataclass
class Corpus:
    """One source + mechanism, plus every hypothesis a test mints.

    Every id is `TST_`-prefixed and minted fresh, so teardown can only ever
    remove rows this fixture created: the live database holds a real pipeline
    run that must survive this suite.
    """

    source_id: str
    mechanism_id: str
    hypothesis_ids: list[str] = field(default_factory=list)

    def new_hypothesis(self) -> str:
        hypothesis_id = _rid("TST_HYP")
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO hypotheses
                    (hypothesis_id, mechanism_id, statement, expected_mechanism,
                     candidate_variables)
                VALUES (%s, %s,
                        'Persistent signed volume imbalance predicts drift',
                        'Informed makers consume the imbalance before it '
                        'reverses, so the pressure shows up as drift first',
                        '{signed_volume_imbalance_1m,microprice_deviation_bps}')
                """,
                (hypothesis_id, self.mechanism_id),
            )
        self.hypothesis_ids.append(hypothesis_id)
        return hypothesis_id

    def set_temporal_context(self, risk: str) -> None:
        """Record point-in-time provenance for the corpus source."""
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO source_temporal_context
                    (source_id, publication_date, information_available_at,
                     future_information_risk)
                VALUES (%s, now() - interval '30 days',
                        now() - interval '30 days', %s)
                ON CONFLICT (source_id) DO UPDATE
                    SET future_information_risk = EXCLUDED.future_information_risk
                """,
                (self.source_id, risk),
            )

    def delete_all(self) -> None:
        # Children before parents, or the FK refuses and the whole teardown
        # rolls back -- leaving the database dirty for the next test.
        ids = self.hypothesis_ids
        with db.tx() as cur:
            cur.execute(
                "DELETE FROM hypothesis_quality WHERE hypothesis_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM research_generations "
                "WHERE hypothesis_id = ANY(%s) OR parent_hypothesis_id = ANY(%s)",
                (ids, ids),
            )
            cur.execute(
                "DELETE FROM hypotheses WHERE hypothesis_id = ANY(%s)", (ids,)
            )
            cur.execute(
                "DELETE FROM source_temporal_context WHERE source_id = %s",
                (self.source_id,),
            )
            cur.execute(
                "DELETE FROM research_notes WHERE mechanism_id = %s",
                (self.mechanism_id,),
            )
            cur.execute(
                "DELETE FROM sources WHERE source_id = %s", (self.source_id,)
            )


@pytest.fixture
def corpus() -> Corpus:
    made = Corpus(source_id=_rid("TST_SRC"), mechanism_id=_rid("TST_MCH"))
    try:
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO sources
                    (source_id, source_type, title, content, content_sha256,
                     date, tier)
                VALUES (%s, 'note', %s, %s, %s, now(), '2')
                """,
                (
                    made.source_id,
                    "Quality test evidence",
                    "Informed makers absorb aggressive signed flow, and the "
                    "resulting price pressure persists briefly before reversing.",
                    uuid.uuid4().hex,
                ),
            )
            cur.execute(
                """
                INSERT INTO research_notes
                    (mechanism_id, source_id, mechanism_name, observation,
                     mechanism_description, candidate_variables)
                VALUES (%s, %s, 'Liquidity absorption',
                        'aggressive signed flow is absorbed rather than passed on',
                        'A liquidity provider whose inventory constraint binds '
                        'consumes the imbalance, so pressure appears as drift '
                        'before the book restores',
                        '{signed_volume_imbalance_1m,microprice_deviation_bps}')
                """,
                (made.mechanism_id, made.source_id),
            )
        yield made
    finally:
        made.delete_all()


def _run_with_deadline(fn, seconds: float = 10.0):
    """Run `fn` on a daemon thread and fail rather than hang."""
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"call did not terminate within {seconds}s -- infinite walk")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("result")


# ---------------------------------------------------------------------
# 1. Bands
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "band"),
    [
        (0, "reject"),
        (20, "reject"),
        (21, "weak"),
        (30, "weak"),
        (31, "experiment"),
        (35, "experiment"),
        (36, "high_priority"),
        (40, "high_priority"),
    ],
)
def test_band_boundaries_are_inclusive_at_the_bottom(
    total: int, band: str, corpus: Corpus
) -> None:
    """20/21, 30/31 and 35/36 are the three off-by-one seams.

    Written through `score_hypothesis` rather than asserted on a helper, so
    the boundary is proved against the deployed `priority_band` CHECK as well
    as against the Python that computed it.
    """
    hypothesis_id = corpus.new_hypothesis()
    scored = quality.score_hypothesis(hypothesis_id, scores=_scores_for(total))

    assert scored.total_score == total
    assert scored.priority_band == band

    stored = quality.get_score(hypothesis_id)
    assert stored is not None
    assert (stored.total_score, stored.priority_band) == (total, band)


def test_every_band_starts_where_the_previous_one_ends(corpus: Corpus) -> None:
    """The four lower bounds are 0/21/31/36 -- an off-by-one in the table
    would make two bands overlap and one range unreachable."""
    assert [lower for lower, _ in PRIORITY_BANDS] == [0, 21, 31, 36]
    assert [name for _, name in PRIORITY_BANDS] == [
        "reject",
        "weak",
        "experiment",
        "high_priority",
    ]


# ---------------------------------------------------------------------
# 2. The total is derived, never supplied
# ---------------------------------------------------------------------


def test_total_is_the_sum_of_the_eight_dimensions(corpus: Corpus) -> None:
    hypothesis_id = corpus.new_hypothesis()
    block = {dim: 3 for dim in QUALITY_DIMENSIONS}
    block["confounding_risk"] = 1  # higher means lower risk; verify it adds
    scored = quality.score_hypothesis(hypothesis_id, scores=block)

    assert scored.total_score == sum(scored.dimensions().values())
    assert scored.total_score == 3 * 7 + 1


def test_confounding_risk_is_oriented_so_higher_helps(corpus: Corpus) -> None:
    """The one dimension whose name reads the other way.

    If this ever inverts, a hypothesis riddled with confounders starts scoring
    well and the total becomes a number that cannot be compared with anything.
    The database cannot catch it: it checks the sum, not the meaning.
    """
    hypothesis_id = corpus.new_hypothesis()
    low = {dim: 2 for dim in QUALITY_DIMENSIONS}
    high = {dim: 2 for dim in QUALITY_DIMENSIONS}
    low["confounding_risk"] = 0
    high["confounding_risk"] = 5

    assert (
        quality.score_hypothesis(hypothesis_id, scores=high).total_score
        > quality.score_hypothesis(hypothesis_id, scores=low).total_score
    )


def test_database_rejects_a_mis_summed_row(corpus: Corpus) -> None:
    """The arithmetic guard is live in the deployed database, not just in the
    schema file: a hand-written total that disagrees with the eight dimensions
    is refused, which is exactly why `score_hypothesis` computes the sum."""
    hypothesis_id = corpus.new_hypothesis()
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as cur:
            cur.execute(
                """
                INSERT INTO hypothesis_quality
                    (hypothesis_id, mechanism_clarity, falsifiability,
                     evidence_quality, novelty, data_availability,
                     temporal_integrity, implementation_clarity,
                     confounding_risk, total_score, priority_band)
                VALUES (%s, 2, 2, 2, 2, 2, 2, 2, 2, 99, 'high_priority')
                """,
                (hypothesis_id,),
            )


def test_rescoring_replaces_the_previous_row(corpus: Corpus) -> None:
    """`hypothesis_quality` is keyed by hypothesis, so a re-score is an update
    of the standing verdict rather than a second, contradictory verdict."""
    hypothesis_id = corpus.new_hypothesis()
    quality.score_hypothesis(hypothesis_id, scores=_scores_for(10))
    quality.score_hypothesis(hypothesis_id, scores=_scores_for(38))

    stored = quality.get_score(hypothesis_id)
    assert stored is not None
    assert (stored.total_score, stored.priority_band) == (38, "high_priority")

    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM hypothesis_quality WHERE hypothesis_id = %s",
            (hypothesis_id,),
        )
        assert cur.fetchone()["n"] == 1


# ---------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 6, 100])
def test_out_of_range_dimension_is_rejected(bad: int, corpus: Corpus) -> None:
    hypothesis_id = corpus.new_hypothesis()
    block = {dim: 3 for dim in QUALITY_DIMENSIONS}
    block["novelty"] = bad

    with pytest.raises(DimensionError):
        quality.score_hypothesis(hypothesis_id, scores=block)
    # Nothing was written: a rejected score must not leave a partial verdict.
    assert quality.get_score(hypothesis_id) is None


def test_non_integer_dimension_is_rejected(corpus: Corpus) -> None:
    """`True` is an int in Python.  Letting it through would score novelty 1
    for a caller who meant a boolean flag."""
    hypothesis_id = corpus.new_hypothesis()
    block = {dim: 3 for dim in QUALITY_DIMENSIONS}
    block["novelty"] = True

    with pytest.raises(DimensionError):
        quality.score_hypothesis(hypothesis_id, scores=block)

    block["novelty"] = 4.5
    with pytest.raises(DimensionError):
        quality.score_hypothesis(hypothesis_id, scores=block)


def test_unknown_dimension_name_is_rejected(corpus: Corpus) -> None:
    """A typo must fail loudly.  Dropping the key would score it 0 and quietly
    move the hypothesis down a band for no stated reason."""
    hypothesis_id = corpus.new_hypothesis()
    block = {dim: 3 for dim in QUALITY_DIMENSIONS}
    block["profitability"] = 5

    with pytest.raises(DimensionError) as excinfo:
        quality.score_hypothesis(hypothesis_id, scores=block)

    assert "profitability" in str(excinfo.value)
    assert quality.get_score(hypothesis_id) is None


def test_unknown_hypothesis_is_refused_by_name(corpus: Corpus) -> None:
    with pytest.raises(QualityError) as excinfo:
        quality.score_hypothesis(_rid("TST_HYP"), scores=_scores_for(20))
    assert "unknown hypothesis" in str(excinfo.value)


# ---------------------------------------------------------------------
# 4. The deterministic proxy
# ---------------------------------------------------------------------


def test_provisional_score_is_deterministic(corpus: Corpus) -> None:
    """Scored twice from identical stored state, it must return the same
    numbers.  This is why no model is consulted: a rank you cannot reproduce
    is not a ranking."""
    hypothesis_id = corpus.new_hypothesis()
    first = quality.score_hypothesis(hypothesis_id)
    second = quality.score_hypothesis(hypothesis_id)

    assert first.total_score == second.total_score
    assert first.dimensions() == second.dimensions()
    assert first.total_score == sum(second.dimensions().values())


def test_provisional_score_declares_itself_a_proxy(corpus: Corpus) -> None:
    """A row written by the heuristic must never be mistaken later for one a
    reviewer stood behind."""
    hypothesis_id = corpus.new_hypothesis()
    scored = quality.score_hypothesis(hypothesis_id)

    assert scored.rationale is not None
    assert quality.PROVISIONAL_RATIONALE in scored.rationale


def test_recorded_future_information_risk_drives_temporal_integrity(
    corpus: Corpus,
) -> None:
    """The dimension reads the recorded provenance assessment, and an
    unrecorded one scores as `unknown` rather than as clean -- a risk nobody
    assessed must not read as an absence of risk."""
    clean = corpus.new_hypothesis()
    leaky = corpus.new_hypothesis()
    corpus.set_temporal_context("high")

    leaky_score = quality.score_hypothesis(leaky)

    corpus.set_temporal_context("none")
    clean_score = quality.score_hypothesis(clean)

    assert clean_score.temporal_integrity == 5
    assert leaky_score.temporal_integrity == 0
    assert leaky_score.temporal_integrity < clean_score.temporal_integrity


def test_an_unformalized_hypothesis_cannot_score_high(corpus: Corpus) -> None:
    """With no formalized spec there is no falsification condition and nothing
    implemented, so two dimensions have nothing to credit."""
    hypothesis_id = corpus.new_hypothesis()
    scored = quality.score_hypothesis(hypothesis_id)

    assert scored.falsifiability == 0
    assert scored.implementation_clarity == 0


# ---------------------------------------------------------------------
# 5. Ranking
# ---------------------------------------------------------------------


def test_rank_queue_orders_strongest_first(corpus: Corpus) -> None:
    strong = corpus.new_hypothesis()
    weak = corpus.new_hypothesis()
    quality.score_hypothesis(strong, scores=_scores_for(37))
    quality.score_hypothesis(weak, scores=_scores_for(22))

    ranked = quality.rank_queue(limit=200)
    assert (strong, 37, "high_priority") in ranked
    assert (weak, 22, "weak") in ranked
    assert ranked.index((strong, 37, "high_priority")) < ranked.index(
        (weak, 22, "weak")
    )


def test_list_scores_filters_by_band(corpus: Corpus) -> None:
    hypothesis_id = corpus.new_hypothesis()
    quality.score_hypothesis(hypothesis_id, scores=_scores_for(33))

    in_band = quality.list_scores(priority_band="experiment", limit=200)
    assert hypothesis_id in {s.hypothesis_id for s in in_band}

    elsewhere = quality.list_scores(priority_band="high_priority", limit=200)
    assert hypothesis_id not in {s.hypothesis_id for s in elsewhere}


def test_unknown_band_is_rejected(corpus: Corpus) -> None:
    with pytest.raises(QualityError):
        quality.list_scores(priority_band="urgent")


# ---------------------------------------------------------------------
# 6. Lineage
# ---------------------------------------------------------------------


def test_generation_number_is_depth(corpus: Corpus) -> None:
    """A root is 1 and each step down adds one, so the tree depth is a fact
    rather than something a caller asserts."""
    root = corpus.new_hypothesis()
    child = corpus.new_hypothesis()
    grandchild = corpus.new_hypothesis()

    assert quality.record_generation(root, "new", "first idea").generation_number == 1
    assert (
        quality.record_generation(
            child, "refinement", "sharpened horizon", parent_hypothesis_id=root
        ).generation_number
        == 2
    )
    assert (
        quality.record_generation(
            grandchild,
            "specialization",
            "restricted to one regime",
            parent_hypothesis_id=child,
        ).generation_number
        == 3
    )


def test_generation_id_is_prefixed_and_unique(corpus: Corpus) -> None:
    first = quality.record_generation(corpus.new_hypothesis(), "new", "first idea")
    second = quality.record_generation(corpus.new_hypothesis(), "new", "another idea")

    assert first.generation_id.startswith("GEN_")
    assert first.generation_id != second.generation_id


def test_lineage_walks_from_the_child_back_to_the_root(corpus: Corpus) -> None:
    root = corpus.new_hypothesis()
    child = corpus.new_hypothesis()
    grandchild = corpus.new_hypothesis()

    quality.record_generation(root, "new", "first idea")
    quality.record_generation(
        child, "refinement", "sharpened horizon", parent_hypothesis_id=root
    )
    quality.record_generation(
        grandchild, "specialization", "one regime only", parent_hypothesis_id=child
    )

    chain = quality.lineage(grandchild)

    assert [g.hypothesis_id for g in chain] == [grandchild, child, root]
    assert [g.mutation_type for g in chain] == [
        "specialization",
        "refinement",
        "new",
    ]
    assert chain[-1].parent_hypothesis_id is None


def test_lineage_of_an_unrecorded_hypothesis_is_empty(corpus: Corpus) -> None:
    """A guess with no ancestry is not a research artifact, and saying so is
    better than inventing a root for it."""
    assert quality.lineage(corpus.new_hypothesis()) == []


def test_family_of_finds_siblings_and_the_root(corpus: Corpus) -> None:
    root = corpus.new_hypothesis()
    left = corpus.new_hypothesis()
    right = corpus.new_hypothesis()
    grandchild = corpus.new_hypothesis()
    stranger = corpus.new_hypothesis()

    quality.record_generation(root, "new", "first idea")
    quality.record_generation(
        left, "refinement", "sharper threshold", parent_hypothesis_id=root
    )
    quality.record_generation(
        right, "generalization", "wider universe", parent_hypothesis_id=root
    )
    quality.record_generation(
        grandchild, "replication", "same rule, other venue", parent_hypothesis_id=left
    )
    quality.record_generation(stranger, "new", "unrelated family")

    family = quality.family_of(grandchild)

    assert family[0] == root, "the root must come first"
    assert set(family) == {root, left, right, grandchild}
    assert stranger not in family


def test_family_of_an_unrecorded_hypothesis_is_just_itself(corpus: Corpus) -> None:
    hypothesis_id = corpus.new_hypothesis()
    assert quality.family_of(hypothesis_id) == [hypothesis_id]


def test_refine_count_ignores_the_founding_generations(corpus: Corpus) -> None:
    """It counts revisions, not nodes.  The research question is how many
    times a family was sharpened, so counting the root would answer a
    different and less useful question."""
    root = corpus.new_hypothesis()
    second = corpus.new_hypothesis()
    third = corpus.new_hypothesis()
    fourth = corpus.new_hypothesis()

    quality.record_generation(root, "new", "first idea")
    quality.record_generation(
        second, "refinement", "sharper threshold", parent_hypothesis_id=root
    )
    quality.record_generation(
        third, "refinement", "sharper horizon", parent_hypothesis_id=second
    )
    quality.record_generation(
        fourth, "generalization", "wider universe", parent_hypothesis_id=third
    )

    assert quality.refine_count(root) == 3
    assert quality.refine_count(fourth) == 3
    assert quality.refine_count(corpus.new_hypothesis()) == 0


def test_revisions_in_another_family_do_not_inflate_the_count(
    corpus: Corpus,
) -> None:
    """The count is per family.  A busy corpus elsewhere in the tree must not
    make one stubborn mechanism look well-explored."""
    root = corpus.new_hypothesis()
    child = corpus.new_hypothesis()
    quality.record_generation(root, "new", "first idea")
    quality.record_generation(
        child, "refinement", "sharper threshold", parent_hypothesis_id=root
    )

    other_root = corpus.new_hypothesis()
    other_child = corpus.new_hypothesis()
    quality.record_generation(other_root, "new", "other family")
    quality.record_generation(
        other_child, "refinement", "other revision", parent_hypothesis_id=other_root
    )

    assert quality.refine_count(child) == 1


def test_list_generations_filters_by_parent(corpus: Corpus) -> None:
    root = corpus.new_hypothesis()
    left = corpus.new_hypothesis()
    right = corpus.new_hypothesis()
    quality.record_generation(root, "new", "first idea")
    quality.record_generation(
        left, "refinement", "sharper threshold", parent_hypothesis_id=root
    )
    quality.record_generation(
        right, "refinement", "sharper horizon", parent_hypothesis_id=root
    )

    children = quality.list_generations(parent_hypothesis_id=root)
    assert {g.hypothesis_id for g in children} == {left, right}

    everything = quality.list_generations(limit=200)
    assert {g.hypothesis_id for g in everything} >= {root, left, right}


# ---------------------------------------------------------------------
# 7. Refusals
# ---------------------------------------------------------------------


def test_unknown_mutation_type_is_rejected(corpus: Corpus) -> None:
    """A revision that cannot be typed is not a revision: it is a hypothesis
    wearing a parent's name, and lineage contamination is worse than an
    honest orphan."""
    hypothesis_id = corpus.new_hypothesis()
    with pytest.raises(MutationTypeError) as excinfo:
        quality.record_generation(hypothesis_id, "improved", "made it better")

    assert "improved" in str(excinfo.value)
    # The refusal must leave no half-recorded lineage behind.
    assert quality.lineage(hypothesis_id) == []


def test_an_empty_revision_reason_is_rejected(corpus: Corpus) -> None:
    """An unjustified revision is a re-run, not progress."""
    with pytest.raises(MutationTypeError):
        quality.record_generation(corpus.new_hypothesis(), "new", "   ")


def test_all_seven_mutation_types_are_accepted(corpus: Corpus) -> None:
    for index, mutation_type in enumerate(quality.MUTATION_TYPES):
        parent = corpus.new_hypothesis() if index else None
        hypothesis_id = corpus.new_hypothesis()
        generation = quality.record_generation(
            hypothesis_id, mutation_type, f"mutation {mutation_type}",
            parent_hypothesis_id=parent,
        )
        assert generation.mutation_type == mutation_type


# ---------------------------------------------------------------------
# 8. Cycle safety
# ---------------------------------------------------------------------


def _build_cycle(corpus: Corpus) -> tuple[str, str]:
    """A hand-made cycle: A's latest generation points at B, B's at A.

    Built through the public API on purpose -- `record_generation` never writes
    one, but the table is readable and writable by anything holding a DSN, and
    a restored or imported database is exactly where a cycle would appear.
    """
    first = corpus.new_hypothesis()
    second = corpus.new_hypothesis()
    quality.record_generation(first, "new", "first idea")
    quality.record_generation(
        second, "refinement", "sharpened threshold", parent_hypothesis_id=first
    )
    quality.record_generation(
        first, "refinement", "restored from a backup", parent_hypothesis_id=second
    )
    return first, second


def test_lineage_terminates_on_a_cycle(corpus: Corpus) -> None:
    """Without a visited set this walk never returns.  There is no exception to
    assert here, so the deadline on the helper thread is the assertion."""
    first, second = _build_cycle(corpus)

    chain = _run_with_deadline(lambda: quality.lineage(first))

    # The walk visits NODES, not edges: it stops the moment it would revisit
    # `first`, so the root is not re-emitted a second time.  It terminates,
    # which is the property under test; the length is incidental.
    assert [g.hypothesis_id for g in chain] == [first, second]


def test_family_of_terminates_on_a_cycle(corpus: Corpus) -> None:
    """The same guard is needed in SQL: a descendant set that deduplicates on
    `(id, depth)` re-admits `A` at a new depth and recurses forever."""
    first, second = _build_cycle(corpus)

    family = _run_with_deadline(lambda: quality.family_of(first))

    assert set(family) == {first, second}


def test_refine_count_terminates_on_a_cycle(corpus: Corpus) -> None:
    first, _ = _build_cycle(corpus)
    # Three generations exist in this family -- the founding 'new' plus two
    # refinements -- so the family has been revised twice.
    assert _run_with_deadline(lambda: quality.refine_count(first)) == 2


# ---------------------------------------------------------------------
# 9. What this module must never grow
# ---------------------------------------------------------------------


def test_quality_score_carries_no_trading_performance_field(corpus: Corpus) -> None:
    """The structural guard on the thing this whole module exists to prevent.

    Layer 1 must not be able to see a profitability target, because a research
    layer that can see one generates hypotheses tuned to flatter a backtest
    instead of hypotheses that are true.  Layer 2 decides whether a hypothesis
    makes money; a hypothesis can score 40/40 here and still lose.
    """
    forbidden = ("return", "sharpe", "drawdown", "profit", "pnl", "hit_rate")
    for model in (quality.QualityScore, quality.Generation):
        for name in model.model_fields:
            assert not any(word in name.lower() for word in forbidden), (
                f"{model.__name__}.{name} is a trading-performance field"
            )

    scored = quality.score_hypothesis(corpus.new_hypothesis(), scores=_scores_for(40))
    assert scored.total_score == 40
    assert scored.priority_band == "high_priority"