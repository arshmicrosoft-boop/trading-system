-- =====================================================================
-- MoonBot Layer 1 -- Research Engine
-- Schema v1.0
--
-- BOUNDARY: This schema holds research artifacts only.
-- There are no orders, positions, fills, balances or exchange
-- credentials anywhere in this database. Layer 1 cannot trade.
--
-- This script is idempotent: applying it to a fresh or an already
-- populated database both succeed, so it can run on every service boot.
--
-- Deviations from the draft schema supplied in the specification, and why:
--   * TIMESTAMP -> TIMESTAMPTZ. Every timestamp here is provenance metadata
--     compared across machines and against market data; a naive timestamp
--     silently corrupts those comparisons.
--   * Free-text status/source_type -> native ENUM, so the database cannot
--     accept an unparseable lifecycle state that Layer 2 would have to
--     defend against.
--   * Added research_identity plus experiment_queue.version_id, because the
--     research_id contract requires source_ids / creation_timestamp /
--     model_used / prompt_version, which none of the six draft tables hold.

BEGIN;

-- ---------------------------------------------------------------------
-- Enumerated domains
-- ---------------------------------------------------------------------
DO $$ BEGIN
CREATE TYPE source_type AS ENUM (
    'youtube', 'pdf', 'github', 'article', 'note', 'manual'
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Tier 1 peer-reviewed .. Tier 5 LLM-generated. Lower tier == stronger
-- evidentiary standing. Provenance weight only; never a pass/fail gate.
DO $$ BEGIN
CREATE TYPE source_tier AS ENUM ('1', '2', '3', '4', '5');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE hypothesis_status AS ENUM (
    'proposed',    -- generated, not yet reviewed
    'critiqued',   -- Research Critic has ruled on it
    'rejected',    -- critic verdict = FAIL
    'formalized',  -- machine-readable spec exists
    'queued'       -- admitted to the experiment queue
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE experiment_status AS ENUM (
    'queued',    -- waiting for Layer 2
    'claimed',   -- Layer 2 has taken it
    'completed'  -- Layer 2 reported a terminal result
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE failure_type AS ENUM (
    'implausible_mechanism',
    'not_measurable',
    'not_falsifiable',
    'not_distinct',
    'hidden_discretion',
    'future_information_leak',
    'prior_failure_precedent',
    'unformalizable',
    'malformed_llm_output'
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- Identifier sequences.
--
-- IDs are sequential (SRC_001, MECH_001, H0001, EXP_001) because the
-- research database is read by people first and Layer 2 second. Allocation
-- goes through Postgres so concurrent writers cannot collide -- that is the
-- whole reason these are sequences rather than COUNT(*)+1.
-- ---------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS id_seq_source     START 1;
CREATE SEQUENCE IF NOT EXISTS id_seq_mechanism  START 1;
CREATE SEQUENCE IF NOT EXISTS id_seq_hypothesis START 1;
CREATE SEQUENCE IF NOT EXISTS id_seq_experiment  START 1;

-- ---------------------------------------------------------------------
-- 1. Sources -- immutable evidence
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    source_id      TEXT PRIMARY KEY,
    source_type    source_type      NOT NULL,
    title          TEXT             NOT NULL,
    author         TEXT,
    url            TEXT,
    content        TEXT             NOT NULL,
    content_sha256 TEXT             NOT NULL,
    date           TIMESTAMPTZ      NOT NULL,
    tier           source_tier      NOT NULL,
    ingested_at    TIMESTAMPTZ      NOT NULL DEFAULT now()
);

COMMENT ON TABLE  sources IS 'Immutable research evidence. UPDATE is blocked by trigger.';
COMMENT ON COLUMN sources.tier IS '1 peer-reviewed, 2 practitioner, 3 blog, 4 YouTube, 5 LLM-generated.';
COMMENT ON COLUMN sources.content_sha256 IS 'SHA-256 of content; tamper evidence and dedupe key.';

CREATE INDEX IF NOT EXISTS sources_tier_idx     ON sources (tier);
CREATE INDEX IF NOT EXISTS sources_type_idx     ON sources (source_type);
CREATE INDEX IF NOT EXISTS sources_sha256_idx   ON sources (content_sha256);
CREATE INDEX IF NOT EXISTS sources_ingested_idx ON sources (ingested_at DESC);

-- Evidence does not get edited. A corrected source is a NEW source.
CREATE OR REPLACE FUNCTION sources_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'sources is append-only: % may not be UPDATEd (immutable evidence)', OLD.source_id
        USING ERRCODE = 'restrict_violation',
              HINT = 'Insert a new source_id instead of mutating existing evidence.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sources_no_update ON sources;
CREATE TRIGGER sources_no_update
    BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION sources_block_mutation();

-- ---------------------------------------------------------------------
-- 2. Mechanisms -- extracted from a source (research_notes)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_notes (
    mechanism_id          TEXT PRIMARY KEY,
    source_id             TEXT        NOT NULL REFERENCES sources(source_id),
    mechanism_name        TEXT        NOT NULL,
    observation           TEXT        NOT NULL,
    mechanism_description TEXT        NOT NULL,
    candidate_variables   TEXT[]      NOT NULL,
    domain               TEXT,
    extraction_model      TEXT,
    prompt_version        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS research_notes_source_idx ON research_notes (source_id);
CREATE INDEX IF NOT EXISTS research_notes_domain_idx ON research_notes (domain);

-- ---------------------------------------------------------------------
-- 3. Hypotheses -- falsifiable claims derived from a mechanism
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id       TEXT PRIMARY KEY,
    mechanism_id        TEXT             NOT NULL REFERENCES research_notes(mechanism_id),
    statement           TEXT             NOT NULL,
    expected_mechanism  TEXT             NOT NULL,
    candidate_variables TEXT[]           NOT NULL DEFAULT '{}',
    status              hypothesis_status NOT NULL DEFAULT 'proposed',
    generator_model     TEXT,
    prompt_version      TEXT,
    created_at          TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hypotheses_mechanism_idx ON hypotheses (mechanism_id);
CREATE INDEX IF NOT EXISTS hypotheses_status_idx    ON hypotheses (status);

-- True when `doc` contains any of `keys` as an object member name, at any
-- depth. Used to keep a profitability target out of a stored rule spec.
--
-- Written as a recursive walk rather than jsonb_path_query: the '$**'
-- jsonpath expands through scalars as well as objects, and .keyvalue() then
-- errors on the scalar members ("can only be applied to an object"), which
-- would make the check fail on legitimate specs that contain numbers or
-- nulls. jsonb_each over a CASE-guarded node sidesteps that entirely.
--
-- IMMUTABLE because a CHECK constraint may not reference a volatile
-- function; the function is pure so that is honest, not a workaround.
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

-- ---------------------------------------------------------------------
-- 4. Hypothesis versions -- immutable formalized YAML specs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hypothesis_versions (
    version_id         SERIAL PRIMARY KEY,
    hypothesis_id      TEXT        NOT NULL REFERENCES hypotheses(hypothesis_id),
    yaml_spec          JSONB       NOT NULL,
    spec_sha256        TEXT        NOT NULL,
    model_used         TEXT        NOT NULL,
    prompt_version     TEXT        NOT NULL,
    creation_timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hypothesis_id, spec_sha256),
    -- Defence in depth for the central architectural boundary. RuleSpec's
    -- extra="forbid" rejects these keys in Python, but the column is JSONB,
    -- so anything writing SQL directly could still smuggle one in -- and
    -- this table is the durable record of what was tested. The check
    -- recurses into nested objects AND arrays, so a hidden key is caught too.
    CONSTRAINT hypothesis_versions_no_profit_target
        CHECK (NOT jsonb_has_forbidden_key(yaml_spec, ARRAY[
            'expected_return', 'target_return', 'expected_profit',
            'target_sharpe', 'expected_sharpe', 'profit_target',
            'return_target', 'expected_pnl']))
);

CREATE INDEX IF NOT EXISTS hypothesis_versions_hyp_idx ON hypothesis_versions (hypothesis_id);

-- A formalized spec is frozen. Re-formalizing the same spec is a no-op
-- (content-addressed); a changed spec inserts a new version.
CREATE OR REPLACE FUNCTION hypothesis_versions_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'hypothesis_versions is append-only: version % may not be changed', OLD.version_id
        USING ERRCODE = 'restrict_violation',
              HINT = 'Insert a new version_id instead.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS hypothesis_versions_no_update ON hypothesis_versions;
CREATE TRIGGER hypothesis_versions_no_update
    BEFORE UPDATE ON hypothesis_versions
    FOR EACH ROW EXECUTE FUNCTION hypothesis_versions_block_mutation();

-- ---------------------------------------------------------------------
-- 5. Research identity -- the provenance contract for every hypothesis
--
-- One row per (hypothesis, rule_version) pair. This is the object the
-- specification calls "research_id": the tuple that makes a result
-- reproducible months later. Note there is deliberately no expected_return
-- column -- Layer 1 does not encode a profitability target.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_identity (
    research_id        TEXT PRIMARY KEY,
    hypothesis_id      TEXT        NOT NULL REFERENCES hypotheses(hypothesis_id),
    source_ids         TEXT[]      NOT NULL,
    feature_set        TEXT[]      NOT NULL,
    rule_version       TEXT        NOT NULL,
    data_version       TEXT        NOT NULL,
    parameter_space    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    creation_timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_used         TEXT        NOT NULL,
    prompt_version     TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS research_identity_hyp_idx  ON research_identity (hypothesis_id);
CREATE INDEX IF NOT EXISTS research_identity_rule_idx ON research_identity (rule_version);
CREATE INDEX IF NOT EXISTS research_identity_src_idx  ON research_identity USING GIN (source_ids);

-- ---------------------------------------------------------------------
-- 6. Experiment queue -- the Layer 1 / Layer 2 boundary
--
-- A row appears here ONLY if the Research Critic returned PASS.
-- Layer 2 reads this table. Layer 1 never writes a result here.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment_queue (
    experiment_id   TEXT PRIMARY KEY,
    research_id     TEXT             NOT NULL UNIQUE REFERENCES research_identity(research_id),
    hypothesis_id   TEXT             NOT NULL REFERENCES hypotheses(hypothesis_id),
    version_id      INTEGER          NOT NULL REFERENCES hypothesis_versions(version_id),
    feature_set     TEXT[]           NOT NULL,
    rule_version    TEXT             NOT NULL,
    data_version    TEXT             NOT NULL,
    parameter_space JSONB            NOT NULL DEFAULT '{}'::jsonb,
    status          experiment_status NOT NULL DEFAULT 'queued',
    queued_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    claimed_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS experiment_queue_status_idx  ON experiment_queue (status);
CREATE INDEX IF NOT EXISTS experiment_queue_hyp_idx     ON experiment_queue (hypothesis_id);
CREATE INDEX IF NOT EXISTS experiment_queue_queued_idx  ON experiment_queue (queued_at);

-- Layer 1 may only enqueue. Claiming/completion is Layer 2's write, but it
-- is allowed here so the queue is a genuine hand-off surface rather than a
-- read-only export. The identity columns Layer 1 wrote stay frozen.
CREATE OR REPLACE FUNCTION experiment_queue_protect_layer1_fields() RETURNS trigger AS $$
BEGIN
    IF NEW.experiment_id     IS DISTINCT FROM OLD.experiment_id
       OR NEW.research_id     IS DISTINCT FROM OLD.research_id
       OR NEW.hypothesis_id   IS DISTINCT FROM OLD.hypothesis_id
       OR NEW.version_id      IS DISTINCT FROM OLD.version_id
       OR NEW.feature_set     IS DISTINCT FROM OLD.feature_set
       OR NEW.rule_version    IS DISTINCT FROM OLD.rule_version
       OR NEW.data_version    IS DISTINCT FROM OLD.data_version
       OR NEW.parameter_space IS DISTINCT FROM OLD.parameter_space
       OR NEW.queued_at       IS DISTINCT FROM OLD.queued_at
    THEN
        RAISE EXCEPTION
            'experiment_queue identity fields are immutable after enqueue (experiment_id=%)',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS experiment_queue_immutable_identity ON experiment_queue;
CREATE TRIGGER experiment_queue_immutable_identity
    BEFORE UPDATE ON experiment_queue
    FOR EACH ROW EXECUTE FUNCTION experiment_queue_protect_layer1_fields();

-- ---------------------------------------------------------------------
-- 7. Failures -- first-class negative knowledge
--
-- The Research Critic writes here when it rejects a hypothesis. Layer 2
-- writes here when a backtest refutes one. Both are first-class: a failure
-- is a result, not an error.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failures (
    failure_id    SERIAL PRIMARY KEY,
    hypothesis_id TEXT         NOT NULL REFERENCES hypotheses(hypothesis_id),
    reason        TEXT         NOT NULL,
    failure_type  failure_type NOT NULL,
    stage         TEXT         NOT NULL DEFAULT 'critic',
    reported_by   TEXT         NOT NULL DEFAULT 'layer1.research_critic',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT failures_stage_known CHECK (stage IN ('critic', 'layer2_backtest'))
);

CREATE INDEX IF NOT EXISTS failures_hyp_idx     ON failures (hypothesis_id);
CREATE INDEX IF NOT EXISTS failures_type_idx    ON failures (failure_type);
CREATE INDEX IF NOT EXISTS failures_created_idx ON failures (created_at DESC);

-- ---------------------------------------------------------------------
-- Layer 2 hand-off view.
--
-- Everything Layer 2 needs to run one experiment, in one row: the rule,
-- its provenance, and where the idea came from.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW experiment_queue_layer2 AS
SELECT
    q.experiment_id,
    q.research_id,
    q.status,
    q.queued_at,
    q.feature_set,
    q.rule_version,
    q.data_version,
    q.parameter_space,
    q.version_id,
    h.hypothesis_id,
    h.statement          AS hypothesis_statement,
    h.expected_mechanism AS expected_mechanism,
    h.candidate_variables,
    v.yaml_spec          AS rule_spec,
    r.source_ids,
    r.model_used,
    r.prompt_version,
    r.creation_timestamp
FROM experiment_queue q
JOIN hypotheses          h ON h.hypothesis_id = q.hypothesis_id
JOIN hypothesis_versions v ON v.version_id     = q.version_id
JOIN research_identity   r ON r.research_id   = q.research_id;

COMMIT;