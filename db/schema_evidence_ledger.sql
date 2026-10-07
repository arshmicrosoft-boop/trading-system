-- =====================================================================
-- MoonBot Layer 1 -- Research Engine
-- Evidence ledger migration (additive)
--
-- BOUNDARY: research artifacts only. Same no-trading boundary as
-- schema.sql; nothing below places an order, a position or a
-- credential anywhere in this database.
--
-- ORDERING: apply AFTER db/schema.sql. Every object here references
-- sources(), hypotheses() and research_identity() from that file.
-- schema.sql is left untouched -- sources carries an append-only
-- trigger and is the schema of record, so all new provenance lives
-- in new tables instead of new columns on existing ones.
--
-- This script is idempotent: applying it to a fresh database, to an
-- already populated one, or twice in a row all succeed, so it can
-- run on every service boot.
--
-- What schema.sql cannot express, and why each gap matters here:
--   * Untyped statements. schema.sql goes straight from a source to a
--     mechanism to a hypothesis, so a statement the LLM wrote can be
--     filed as though it came out of a paper. The claims table types
--     every extracted statement at the source, and 'model_inference'
--     marks the ones this system invented.
--   * Discarded disagreement. Two papers disagreeing is research
--     output; a flat pipeline throws the weaker one away silently.
--     claim_contradictions keeps the conflict and makes it queue work
--     rather than noise.
--   * No gate between critique and enqueue. integrity_audits records
--     the protocol checklist verdict, and the integrity_gate_ready
--     view turns it into an enforceable precondition for hand-off.
--   * A flat pile of hypotheses. research_generations records the
--     evolutionary lineage of an idea, and hypothesis_quality scores
--     the research itself.
--
-- TIMESTAMP -> TIMESTAMPTZ, as in schema.sql: every value here is
-- provenance metadata compared across machines and against market
-- data, and a naive timestamp silently corrupts those comparisons.

BEGIN;

-- ---------------------------------------------------------------------
-- Enumerated domains
-- ---------------------------------------------------------------------
-- The four things a sentence extracted from a source can be. Keeping
-- them in one enum is what stops a fact from being silently promoted
-- to a finding.
DO $$ BEGIN
CREATE TYPE claim_type AS ENUM (
    'fact',              -- directly stated in the source
    'author_claim',      -- the source's assertion, not verified by us
    'empirical_result',  -- a measurement the source reports
    'model_inference'    -- produced by our own LLM, NOT from the source
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE conflict_reason AS ENUM (
    'market',               -- different markets traded
    'timeframe',            -- different horizons over the same effect
    'cost_model',           -- transaction costs modelled differently
    'feature_definition',   -- same name, different construction
    'sampling_frequency',   -- same signal sampled at different rates
    'regime'                -- different volatility/trend regime
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE integrity_status AS ENUM ('pass', 'incomplete', 'fail');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE mutation_type AS ENUM (
    'new',           -- first idea in the lineage
    'refinement',    -- same idea, sharper specification
    'generalization',-- widened beyond its origin scope
    'specialization',-- narrowed to one market or regime
    'combination',   -- two existing mechanisms merged
    'contradiction', -- spawned to resolve a source conflict
    'replication'    -- re-test of an existing hypothesis elsewhere
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- 1. Claims -- extracted evidence, typed
-- ---------------------------------------------------------------------
-- A claim is the smallest unit of evidence between a source and the
-- mechanisms derived from it. Claims are written before any
-- interpretation is built on top of them, which is what makes a later
-- 'where did this come from' answerable.
CREATE TABLE IF NOT EXISTS claims (
    claim_id              TEXT PRIMARY KEY,
    source_id             TEXT NOT NULL REFERENCES sources(source_id),
    statement             TEXT NOT NULL,
    claim_type            claim_type NOT NULL,
    section_ref           TEXT,   -- page / section / timestamp anchor
    evidence_strength     TEXT,   -- free-text tier: strong/moderate/weak
    time_context          JSONB,  -- {data_start, data_end, published_at}
    agent_interpretation  TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  claims IS 'Typed statements extracted from a source. UPDATE is blocked by trigger.';
COMMENT ON COLUMN claims.claim_type IS 'fact / author_claim / empirical_result / model_inference. model_inference is OUR output and is never literature evidence.';
COMMENT ON COLUMN claims.section_ref IS 'Locator into the source: page, section or timestamp. Absent means the extractor could not point at it.';
COMMENT ON COLUMN claims.evidence_strength IS 'Free-text provenance tier (strong/moderate/weak). Deliberately not an enum: it is a judgement, not a protocol state.';
COMMENT ON COLUMN claims.time_context IS 'Point-in-time facts: data_start, data_end, published_at. Null when the source did not state them.';
COMMENT ON COLUMN claims.agent_interpretation IS 'Our reading of the claim, kept in its own column so it is never confused with the claim itself.';

CREATE INDEX IF NOT EXISTS claims_type_idx   ON claims (claim_type);
CREATE INDEX IF NOT EXISTS claims_source_idx ON claims (source_id);

-- A claim is what the source said. Correcting our reading of it is a
-- NEW claim; editing one in place would destroy the record of what we
-- believed when a downstream hypothesis was generated.
CREATE OR REPLACE FUNCTION claims_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'claims is append-only: % may not be UPDATEd (immutable evidence)', OLD.claim_id
        USING ERRCODE = 'restrict_violation',
              HINT = 'Insert a new claim_id instead of mutating existing evidence.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS claims_no_update ON claims;
CREATE TRIGGER claims_no_update
    BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_block_mutation();

-- ---------------------------------------------------------------------
-- 2. Claim contradictions -- conflict is research output
-- ---------------------------------------------------------------------
-- Two sources that disagree have told us something neither told us
-- alone: at least one of them is conditional, mismeasured, or stale.
-- That is a lead, so it gets a table and a lifecycle rather than being
-- resolved by whichever note happens to be written last.
CREATE TABLE IF NOT EXISTS claim_contradictions (
    contradiction_id      TEXT PRIMARY KEY,
    claim_a               TEXT NOT NULL REFERENCES claims(claim_id),
    claim_b               TEXT NOT NULL REFERENCES claims(claim_id),
    possible_reasons      conflict_reason[] NOT NULL,
    resolution_status     TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (resolution_status IN ('unresolved', 'hypothesis_generated', 'explained', 'dismissed')),
    spawned_hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT contradictions_distinct_claims CHECK (claim_a <> claim_b),
    UNIQUE (claim_a, claim_b)
);

COMMENT ON TABLE  claim_contradictions IS 'Unresolved or resolved conflicts between two claims. A claim contradicting itself is a data error, not research.';
COMMENT ON COLUMN claim_contradictions.possible_reasons IS 'Candidate explanations for the conflict. An array because several can hold at once and none need be verified to record the conflict.';
COMMENT ON COLUMN claim_contradictions.spawned_hypothesis_id IS 'Hypothesis generated to adjudicate this conflict. Null while resolution_status = unresolved.';
COMMENT ON COLUMN claim_contradictions.claim_a IS 'Ordered with claim_b: the UNIQUE pair de-duplicates the conflict regardless of which claim was seen first.';
COMMENT ON COLUMN claim_contradictions.resolution_status IS 'unresolved -> hypothesis_generated | explained | dismissed. Never silently dropped.';
COMMENT ON CONSTRAINT contradictions_distinct_claims ON claim_contradictions IS
    'A source contradicting itself is a data error (extraction bug, duplicated claim), not a cross-source conflict. Enforced so it is caught at write time instead of being analysed as research.';

CREATE INDEX IF NOT EXISTS claim_contradictions_b_idx         ON claim_contradictions (claim_b);
CREATE INDEX IF NOT EXISTS claim_contradictions_hyp_idx       ON claim_contradictions (spawned_hypothesis_id);
CREATE INDEX IF NOT EXISTS claim_contradictions_status_idx    ON claim_contradictions (resolution_status);

-- FORBIDDEN OPERATION: do not average claim_a and claim_b into a single
-- hedged claim, and do not pick whichever one is convenient. Averaging
-- two conflicting results yields a hedged claim that neither source
-- actually asserts and that is falsifiable by NEITHER result -- it can
-- only ever come out inconclusive, so it teaches us nothing and hides
-- the conflict that was the finding. The contradiction stays explicit
-- until a hypothesis, a regime split or a feature-definition fix
-- explains WHY they disagree.

-- ---------------------------------------------------------------------
-- 3. Integrity audits -- the gate before the experiment queue
-- ---------------------------------------------------------------------
-- One row per audit pass over RESEARCH_INTEGRITY_CHECKLIST.md. This is
-- the last Layer 1 checkpoint before a hypothesis becomes queueable.
CREATE TABLE IF NOT EXISTS integrity_audits (
    audit_id              TEXT PRIMARY KEY,
    hypothesis_id         TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
    status                integrity_status NOT NULL,
    checklist_results     JSONB NOT NULL,   -- per-item pass/fail from the checklist
    missing_protocol_fields TEXT[],         -- which protocol blocks are undefined
    blocking_reasons      TEXT[] NOT NULL,
    audited_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  integrity_audits IS 'Verdict of the research-integrity checklist. Only status = pass may reach integrity_gate_ready and hence the experiment queue.';
COMMENT ON COLUMN integrity_audits.checklist_results IS 'Per-item pass/fail keyed by checklist item, so a later reader can see WHICH item failed, not just that one did.';
COMMENT ON COLUMN integrity_audits.missing_protocol_fields IS 'Undefined blocks of the formal protocol (universe, features, horizon, costs, evaluation). Empty iff the protocol is complete.';
COMMENT ON COLUMN integrity_audits.blocking_reasons IS 'Human-readable reasons the hypothesis is not enqueueable. Empty is permitted only when status = pass.';
COMMENT ON COLUMN integrity_audits.status IS 'pass | incomplete | fail. HARD RULE: RESEARCH_STATUS = INCOMPLETE when the protocol is incomplete, never READY_FOR_BACKTEST. An under-specified protocol cannot be backtested meaningfully -- the result would flatter an arbitrary choice we made while writing it.';

CREATE INDEX IF NOT EXISTS integrity_audits_hyp_idx    ON integrity_audits (hypothesis_id);
CREATE INDEX IF NOT EXISTS integrity_audits_status_idx ON integrity_audits (status);

-- ---------------------------------------------------------------------
-- 4. Research generations -- evolutionary lineage
-- ---------------------------------------------------------------------
-- Moon Dev's RBI loop rewrites ideas in place. Flat, version-less piles
-- make it impossible to tell a sharpening from a replacement, so every
-- hypothesis is a node in a recorded lineage instead.
CREATE TABLE IF NOT EXISTS research_generations (
    generation_id         TEXT PRIMARY KEY,
    -- The hypothesis this generation PRODUCES. Without it the table records
    -- only what each node was derived from, so a child -> row lookup -- the
    -- direction lineage() walks, and the only way to answer "how many times
    -- has this family been revised" -- is impossible. parent is nullable
    -- (a root has none); child is not.
    hypothesis_id         TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
    parent_hypothesis_id  TEXT REFERENCES hypotheses(hypothesis_id),
    generation_number     INTEGER NOT NULL,
    mutation_type         mutation_type NOT NULL,
    revision_reason       TEXT NOT NULL,
    critique_summary      TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Upgrade path. An installation that applied an earlier version of this file
-- has research_generations without hypothesis_id. ADD COLUMN IF NOT EXISTS is
-- a no-op on a database that already has it, so re-running is safe.
ALTER TABLE research_generations
    ADD COLUMN IF NOT EXISTS hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id);

-- Backfill, then tighten to NOT NULL. A row cannot be attributed without a
-- child hypothesis, so an unattributable row is dropped rather than left
-- blocking the constraint: a lineage node with no hypothesis is not a node.
-- On a database created by the current file the table is empty or fully
-- populated already and both statements are no-ops.
DO $up$
DECLARE
    orphans INT;
BEGIN
    UPDATE research_generations g
       SET hypothesis_id = g.parent_hypothesis_id
 WHERE g.hypothesis_id IS NULL
   AND g.parent_hypothesis_id IS NOT NULL;

    SELECT count(*) INTO orphans
      FROM research_generations
     WHERE hypothesis_id IS NULL;

    IF orphans > 0 THEN
        DELETE FROM research_generations WHERE hypothesis_id IS NULL;
    END IF;

    ALTER TABLE research_generations ALTER COLUMN hypothesis_id SET NOT NULL;
END
$up$;

CREATE INDEX IF NOT EXISTS research_generations_child_idx ON research_generations (hypothesis_id);


COMMENT ON TABLE  research_generations IS 'Lineage of an idea across revisions. A refinement and a wholesale replacement must be distinguishable after the fact.';
COMMENT ON COLUMN research_generations.parent_hypothesis_id IS 'Null only for a lineage root; every other node points at what it was derived from.';
COMMENT ON COLUMN research_generations.mutation_type IS 'new | refinement | generalization | specialization | combination | contradiction | replication. The kind of change, not just that one happened.';
COMMENT ON COLUMN research_generations.revision_reason IS 'Why this revision was made. NOT NULL because an unjustified revision is a rerun, not progress.';
COMMENT ON COLUMN research_generations.critique_summary IS 'What the Research Critic objected to in the parent. Null when no critique preceded the revision.';

CREATE INDEX IF NOT EXISTS research_generations_parent_idx ON research_generations (parent_hypothesis_id);
CREATE INDEX IF NOT EXISTS research_generations_number_idx ON research_generations (generation_number);
CREATE INDEX IF NOT EXISTS research_generations_type_idx   ON research_generations (mutation_type);

-- ---------------------------------------------------------------------
-- 5. Hypothesis quality -- scoring the RESEARCH, not the P&L
-- ---------------------------------------------------------------------
-- Eight dimensions, 0-5 each, 0-40 total, banded. This ranks how well
-- an idea is specified and how well it is motivated. It says nothing
-- about money: Layer 2 decides profitability, and a hypothesis can
-- score 40 here and still lose.
CREATE TABLE IF NOT EXISTS hypothesis_quality (
    hypothesis_id          TEXT PRIMARY KEY REFERENCES hypotheses(hypothesis_id),
    mechanism_clarity     INTEGER NOT NULL CHECK (mechanism_clarity     BETWEEN 0 AND 5),
    falsifiability        INTEGER NOT NULL CHECK (falsifiability        BETWEEN 0 AND 5),
    evidence_quality      INTEGER NOT NULL CHECK (evidence_quality      BETWEEN 0 AND 5),
    novelty               INTEGER NOT NULL CHECK (novelty               BETWEEN 0 AND 5),
    data_availability     INTEGER NOT NULL CHECK (data_availability     BETWEEN 0 AND 5),
    temporal_integrity    INTEGER NOT NULL CHECK (temporal_integrity    BETWEEN 0 AND 5),
    implementation_clarity INTEGER NOT NULL CHECK (implementation_clarity BETWEEN 0 AND 5),
    confounding_risk      INTEGER NOT NULL CHECK (confounding_risk      BETWEEN 0 AND 5),
    total_score           INTEGER NOT NULL,
    priority_band         TEXT NOT NULL CHECK (priority_band IN ('reject', 'weak', 'experiment', 'high_priority')),
    -- The reasoning behind the score. Without it a score is an oracle: a
    -- reviewer cannot tell whether 3/5 on falsifiability was argued or
    -- guessed.
    rationale             TEXT,
    scored_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT quality_total_consistent CHECK (total_score =
        mechanism_clarity + falsifiability + evidence_quality + novelty
        + data_availability + temporal_integrity + implementation_clarity + confounding_risk)
);

COMMENT ON TABLE  hypothesis_quality IS
    'RESEARCH QUALITY ONLY -- NOT trading performance. Mechanism clarity, falsifiability, evidence quality, novelty, measurability. This table deliberately contains no return, Sharpe, drawdown or profitability field: Layer 1 has no target to optimise and must not be able to see one, because a research layer that can see a profitability target will generate hypotheses tuned to flatter a backtest rather than to be true. Layer 2 decides profitability; a hypothesis can score 40/40 here and still lose money.';
COMMENT ON COLUMN hypothesis_quality.confounding_risk IS 'Higher = WORSE. 5 means the effect cannot be separated from an alternative explanation, so 5 helps the hypothesis, not hurts it. All eight dimensions share the direction "higher is better" so the total is a single comparable number.';

-- Upgrade path for a database created before `rationale` existed. Placed
-- here, after the CREATE TABLE, because an ALTER against a table that has
-- not been created yet aborts the whole script -- which is exactly what
-- happens on a first-time install.
ALTER TABLE hypothesis_quality ADD COLUMN IF NOT EXISTS rationale TEXT;
COMMENT ON COLUMN hypothesis_quality.priority_band IS '0-20 reject, 21-30 weak, 31-35 experiment, 36-40 high_priority. Bands, not a continuous priority: a rank is a comparison between ideas, a band is a statement about one.';
COMMENT ON COLUMN hypothesis_quality.total_score IS 'Sum of the eight dimensions, enforced by constraint so an inconsistent score is unrepresentable rather than merely discouraged.';
COMMENT ON CONSTRAINT quality_total_consistent ON hypothesis_quality IS
    'The total is derived, not supplied independently. Storing it while constraining the arithmetic makes a hand-edited or mis-summed score impossible to represent, so a downstream reader never has to recompute it to know it can trust it.';

CREATE INDEX IF NOT EXISTS hypothesis_quality_band_idx   ON hypothesis_quality (priority_band);
CREATE INDEX IF NOT EXISTS hypothesis_quality_score_idx  ON hypothesis_quality (total_score DESC);

-- ---------------------------------------------------------------------
-- 6. Source temporal context -- point-in-time provenance
--
-- A separate table rather than columns on sources: sources carries an
-- append-only trigger and is the schema of record, so it is left
-- exactly as schema.sql defines it.
-- ---------------------------------------------------------------------
-- An undated source is normal (an LLM note has no publication date),
-- so publication_date is nullable while information_available_at is
-- not: the moment the content could first have been known is what a
-- backtest must respect, and we must be able to record that we do not
-- know it.
CREATE TABLE IF NOT EXISTS source_temporal_context (
    source_id                  TEXT PRIMARY KEY REFERENCES sources(source_id),
    publication_date           TIMESTAMPTZ,
    information_available_at   TIMESTAMPTZ NOT NULL,
    future_information_risk   TEXT NOT NULL DEFAULT 'unknown'
        CHECK (future_information_risk IN ('none', 'low', 'medium', 'high', 'unknown'))
);

COMMENT ON TABLE  source_temporal_context IS 'Point-in-time provenance per source: when it was published and when its content could first have been known. Separate from sources to leave the append-only table of record untouched.';
COMMENT ON COLUMN source_temporal_context.publication_date IS 'Declared publication date. Null when the source states none; absence is recorded rather than guessed.';
COMMENT ON COLUMN source_temporal_context.information_available_at IS 'The earliest time this content could have informed a decision. This, not publication_date, bounds what a backtest may use.';
COMMENT ON COLUMN source_temporal_context.future_information_risk IS 'none | low | medium | high | unknown. Assessed BEFORE testing. Defaults to unknown because unknown is the honest state, and a risk never assessed must not read as absent.';

CREATE INDEX IF NOT EXISTS source_temporal_context_risk_idx ON source_temporal_context (future_information_risk);

-- ---------------------------------------------------------------------
-- 7. Integrity gate view -- the Layer 1 / Layer 2 precondition
--
-- One row per hypothesis whose LATEST audit passed. The latest audit is
-- taken over ALL statuses and only then filtered to 'pass', rather than
-- taking the latest passing audit. That distinction is the whole safety
-- property: if a hypothesis once passed and is re-audited to 'fail' or
-- 'incomplete', the newest verdict is not a pass, so the hypothesis
-- leaves this view and cannot be enqueued. Taking the latest *pass*
-- instead would let a later failure be overridden by the earlier pass,
-- and the gate would fail open exactly when it matters most.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW integrity_gate_ready AS
SELECT
    h.hypothesis_id,
    h.status              AS hypothesis_status,
    h.statement           AS hypothesis_statement,
    h.expected_mechanism,
    h.candidate_variables,
    a.audit_id,
    a.audited_at,
    a.checklist_results,
    a.missing_protocol_fields
FROM hypotheses h
JOIN LATERAL (
    SELECT
        ia.audit_id,
        ia.audited_at,
        ia.status,
        ia.checklist_results,
        ia.missing_protocol_fields
    FROM integrity_audits ia
    WHERE ia.hypothesis_id = h.hypothesis_id
    ORDER BY ia.audited_at DESC, ia.audit_id DESC
    LIMIT 1
) a ON a.status = 'pass';

-- ---------------------------------------------------------------------
-- 8. Profit-target guard on an already-deployed database
--
-- schema.sql carries this CHECK inside CREATE TABLE, but a table that
-- already exists is skipped by CREATE TABLE IF NOT EXISTS. An installation
-- that applied the previous version of schema.sql therefore never got the
-- constraint, so it is added here as well. NOT VALID would be the cheap
-- option; it is deliberately not used, because the point is to guarantee
-- every stored spec is clean.
--
-- The helper is defined here as well as in schema.sql, because a database
-- that applied the previous schema.sql never had it and a CHECK cannot
-- reference a function that does not yet exist.
--
-- Note it must be a SCALAR immutable function: CHECK rejects set-returning
-- functions outright, which is why this does not use jsonb_path_query
-- despite that looking like the natural tool. See the comment on the
-- definition in schema.sql for why $.**.keyvalue() is unusable here.
CREATE OR REPLACE FUNCTION jsonb_has_forbidden_key(doc jsonb, keys text[])
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
WITH RECURSIVE walk(node, key) AS (
    SELECT doc, NULL::text
  UNION ALL
    SELECT x.child, x.k
    FROM walk w
    CROSS JOIN LATERAL (
        SELECT e.key AS k, e.value AS child
        FROM jsonb_each(CASE WHEN jsonb_typeof(w.node) = 'object'
                             THEN w.node ELSE '{}'::jsonb END) e
        UNION ALL
        SELECT NULL::text, a.value
        FROM jsonb_array_elements(CASE WHEN jsonb_typeof(w.node) = 'array'
                                      THEN w.node ELSE '[]'::jsonb END) a
    ) x
)
SELECT EXISTS (SELECT 1 FROM walk WHERE key IS NOT NULL AND lower(key) = ANY(keys));
$fn$;

ALTER TABLE hypothesis_versions DROP CONSTRAINT IF EXISTS hypothesis_versions_no_profit_target;

ALTER TABLE hypothesis_versions
    ADD CONSTRAINT hypothesis_versions_no_profit_target
        CHECK (NOT jsonb_has_forbidden_key(yaml_spec, ARRAY[
            'expected_return', 'target_return', 'expected_profit',
            'target_sharpe', 'expected_sharpe', 'profit_target',
            'return_target', 'expected_pnl']));

COMMENT ON CONSTRAINT hypothesis_versions_no_profit_target ON hypothesis_versions IS
    'Layer 1 has no notion of a good return. A profitability target in a stored rule is a research-layer optimization objective, which would make the research agent generate hypotheses that flatter a backtest rather than hypotheses that are true. RuleSpec.extra="forbid" blocks this in Python; this constraint blocks it for anything writing SQL directly, because this table is the durable record of what was tested.';

COMMENT ON VIEW integrity_gate_ready IS
    'Hypotheses whose latest integrity audit passed. THIS VIEW IS THE MECHANISM THAT PREVENTS AN UNDER-SPECIFIED PROTOCOL FROM REACHING LAYER 2: the queueing layer may only enqueue from this view, so a hypothesis whose protocol is incomplete is not enqueueable at all, and a hypothesis that later fails re-audit is withdrawn from it automatically. The audit is the gate, not a convention -- an integrity verdict of pass is the sole precondition for the Layer 1 / Layer 2 hand-off.';

COMMIT;