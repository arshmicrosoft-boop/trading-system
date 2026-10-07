-- =====================================================================
-- MoonBot Layer 2 -- Experiment execution and results
-- Schema v1.0 (applies after schema.sql + schema_evidence_ledger.sql)
--
-- This is the piece that CLOSES THE LOOP:
--
--     source -> evidence -> mechanism -> hypothesis -> critique
--             -> rulespec -> integrity gate -> experiment queue
--             -> EXPERIMENT RESULT -> knowledge
--
-- The central design rule: a backtest's output is NEVER an informal blob
-- of stdout text. It lands in experiment_results as typed columns, so
-- downstream consumers query structure rather than parse prose. A result
-- whose headline number cannot be selected by name has not really been
-- recorded -- it has been narrated.
--
-- BOUNDARY: this file adds tables and the adapter contract only. It does
-- NOT add a backtest engine, an exchange client, or anything that trades.
-- The adapter port is defined so an external engine (Moon Dev RBI, or any
-- other) can be attached without this repository ever implementing one.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- Execution status
-- ---------------------------------------------------------------------
DO $$ BEGIN
CREATE TYPE experiment_run_status AS ENUM (
    'pending',      -- queued for a runner, nothing executed yet
    'running',
    'completed',    -- ran to completion; metrics below are trustworthy
    'failed',       -- the run itself broke (code error, missing data)
    'invalidated'   -- completed, then withdrawn (e.g. data correction)
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Why a hypothesis was refuted. Deliberately separate from the Layer 1
-- `failure_type` enum: Layer 1 rejects a claim on reasoning grounds, Layer 2
-- refutes it on data. Conflating them would make "the idea was incoherent" and
-- "the idea was wrong" indistinguishable in the failure database.
DO $$ BEGIN
CREATE TYPE result_outcome AS ENUM (
    'supported',         -- evidence is consistent with the hypothesis
    'refuted',           -- evidence contradicts the hypothesis
    'inconclusive',      -- neither; the test could not discriminate
    'errored'            -- the experiment could not be completed
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
CREATE TYPE execution_backend AS ENUM (
    'stub',         -- deterministic no-op; exists so the loop is testable
    'moondev_rbi',  -- external Moon Dev RBI engine, behind the adapter port
    'external'      -- anything else speaking the adapter contract
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- 1. Experiment runs -- one attempt at testing one hypothesis
--
-- Several runs may exist per experiment_id: a parameter sweep, a re-run
-- after a data correction, or a replication attempt. That multiplicity is the
-- whole point -- "H019 failed" is not a fact, "H019 failed on these three
-- runs" is.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id           TEXT PRIMARY KEY,
    experiment_id    TEXT             NOT NULL REFERENCES experiment_queue(experiment_id),
    hypothesis_id    TEXT             NOT NULL REFERENCES hypotheses(hypothesis_id),
    research_id      TEXT             NOT NULL REFERENCES research_identity(research_id),

    -- THE CONTROLLED-EXPERIMENT METADATA.
    -- Two runs of the same hypothesis are only comparable if every one of
    -- these matches. This is the enforcement of "control everything except
    -- the variable under test"; without it, comparing two hypotheses means
    -- comparing two different environments and calling it science.
    dataset          TEXT,
    dataset_version  TEXT,
    universe         JSONB            NOT NULL DEFAULT '{}'::jsonb,
    time_period      JSONB,           -- {start, end}
    timeframe        TEXT,
    feature_version  TEXT,

    -- Execution assumptions, kept structured so "did they trade at the close
    -- or the next open?" is answerable rather than assumed.
    execution_model  JSONB            NOT NULL DEFAULT '{}'::jsonb,
    cost_model       JSONB            NOT NULL DEFAULT '{}'::jsonb,

    parameters       JSONB            NOT NULL DEFAULT '{}'::jsonb,
    random_seed      INTEGER,

    backend          execution_backend NOT NULL DEFAULT 'stub',
    status           experiment_run_status NOT NULL DEFAULT 'pending',
    outcome          result_outcome,

    -- Reproducibility. A result without these three is not a result.
    code_hash        TEXT,
    config_hash      TEXT,
    spec_hash        TEXT,

    -- Failure diagnosis for a broken or refuted run.
    failure_type     TEXT,
    failure_reason   TEXT,

    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

COMMENT ON TABLE  experiment_runs IS
    'One attempt at testing one hypothesis. An experiment_id may have several runs: a parameter sweep, a re-run after a data correction, or a replication attempt. "H019 failed" is not a fact; "H019 failed on these three runs, all after costs" is.';

CREATE INDEX IF NOT EXISTS experiment_runs_exp_idx  ON experiment_runs (experiment_id);
CREATE INDEX IF NOT EXISTS experiment_runs_hyp_idx  ON experiment_runs (hypothesis_id);
CREATE INDEX IF NOT EXISTS experiment_runs_stat_idx  ON experiment_runs (status);
CREATE INDEX IF NOT EXISTS experiment_runs_outcome_idx ON experiment_runs (outcome);

-- ---------------------------------------------------------------------
-- 2. Results -- typed, never prose
--
-- The headline metrics are COLUMNS. That is the whole point: Layer 2 and
-- every analyst downstream select them by name instead of parsing stdout.
-- JSONB carries the long tail; it never carries a headline number.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment_results (
    run_id           TEXT PRIMARY KEY REFERENCES experiment_runs(run_id),

    -- Returns. gross_return without net_return is a marketing number, so
    -- net_return is the one the analysts read.
    gross_return     DOUBLE PRECISION,
    net_return       DOUBLE PRECISION,

    -- Risk.
    volatility       DOUBLE PRECISION,
    max_drawdown     DOUBLE PRECISION,
    sharpe           DOUBLE PRECISION,
    sortino          DOUBLE PRECISION,

    -- Behaviour.
    trade_count      INTEGER,
    turnover         DOUBLE PRECISION,
    hit_rate         DOUBLE PRECISION,
    avg_holding_bars DOUBLE PRECISION,

    -- Integrity of the TEST ITSELF, not of the strategy. These answer "was
    -- this a fair test?" rather than "was the strategy good?".
    stability        DOUBLE PRECISION,   -- survives sub-period splitting
    regime_dependence DOUBLE PRECISION,  -- works in only one regime
    p_value          DOUBLE PRECISION,
    multiple_testing_adjusted BOOLEAN NOT NULL DEFAULT FALSE,

    -- Attribution. Positive return is NOT alpha: a run can earn from market
    -- beta, style or regime exposure rather than from the claimed mechanism.
    -- residual_alpha is what remains after those are accounted for.
    market_exposure  DOUBLE PRECISION,
    factor_exposure  JSONB            NOT NULL DEFAULT '{}'::jsonb,
    residual_alpha   DOUBLE PRECISION,

    -- Did the observed result turn out to be CONSISTENT WITH the mechanism
    -- the hypothesis claimed? This is the distinction that makes attribution
    -- mean anything: a strategy that made money for reasons unrelated to the
    -- stated mechanism is not a success for that hypothesis.
    mechanism_consistent BOOLEAN,

    -- The long tail. Never a headline metric.
    extra_metrics    JSONB            NOT NULL DEFAULT '{}'::jsonb,

    recorded_at      TIMESTAMPTZ      NOT NULL DEFAULT now()
);

COMMENT ON COLUMN experiment_results.net_return IS
    'Return after the cost model. Analysts read this, not gross_return: a gross number that survives no costs is not a finding.';
COMMENT ON COLUMN experiment_results.residual_alpha IS
    'Return left after market/factor/regime exposure is accounted for. KTD-Fin''s point: positive return is not alpha, and a strategy can be paid entirely by market beta.';
COMMENT ON COLUMN experiment_results.mechanism_consistent IS
    'Whether the result is consistent with the mechanism the hypothesis CLAIMED. A profitable run whose mechanism does not hold is not support for that hypothesis. This is why Layer 1 stores expected_mechanism: it is the key Layer 2 tests the result against.';
COMMENT ON COLUMN experiment_results.multiple_testing_adjusted IS
    'False means this p-value was not corrected for having tested many hypotheses. An uncorrected p-value after a sweep of hundreds is close to meaningless, and a consumer must be able to see that from the row alone.';

-- ---------------------------------------------------------------------
-- 3. Run artifacts -- the reproducible workspace
--
-- An experiment nobody can reproduce is a rumour. This records WHERE the code,
-- the logs and the raw output live, plus their hashes, so a later reader can
-- tell whether a re-run used the same inputs.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment_artifacts (
    run_id           TEXT PRIMARY KEY REFERENCES experiment_runs(run_id),
    workspace_path   TEXT,             -- isolated dir for this run
    stdout           TEXT,
    stderr           TEXT,
    code_hash        TEXT,
    data_hash        TEXT,
    log_path         TEXT,
    artifacts        JSONB            NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

COMMENT ON TABLE experiment_artifacts IS
    'Where a run''s code, logs and raw output live, and their hashes. stdout/stderr are retained for forensics ONLY -- they are never the record of what happened; experiment_results is. A consumer must never have to parse these.';

-- ---------------------------------------------------------------------
-- 4. Experiment trace -- the DAG (RD-Agent''s Trace pattern)
--
-- Research history is a DAG, not a list:
--
--     H001 ──┬── run A (refuted)
--            ├── run B (promising) ──> H001a ──> run C
--            └── run D (contradiction)
--
-- Every node is an experiment attempt; every edge records WHY it was tried.
-- The "why" is the entire value: without a revision_reason a lineage is
-- just a list, and the question "this family has been refined 7 times and
-- still fails out of sample" cannot be answered.
-- ---------------------------------------------------------------------
DO $$ BEGIN
CREATE TYPE trace_edge_reason AS ENUM (
    'initial',          -- first attempt at this hypothesis
    'revision',         -- refined after a result
    'generalization',   -- widened scope
    'specialization',   -- narrowed scope
    'parameter_sweep',  -- same claim, different parameters
    'replication',      -- re-testing a published claim
    'contradiction',    -- triggered by a conflict in the evidence
    'data_correction'   -- previous run used wrong data
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS experiment_trace (
    edge_id           TEXT PRIMARY KEY,
    parent_run_id     TEXT             NOT NULL REFERENCES experiment_runs(run_id),
    child_run_id      TEXT             NOT NULL REFERENCES experiment_runs(run_id),
    child_hypothesis_id TEXT           REFERENCES hypotheses(hypothesis_id),
    reason            trace_edge_reason NOT NULL DEFAULT 'revision',
    rationale         TEXT             NOT NULL,
    created_at        TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT trace_no_self_loop CHECK (parent_run_id <> child_run_id)
);

COMMENT ON TABLE  experiment_trace IS
    'The research DAG. Each row is one experiment attempt derived from an earlier one, with the reason it was derived. The rationale is NOT NULL: an unjustified edge is a rerun, not progress.';
COMMENT ON COLUMN experiment_trace.rationale IS
    'Why this run was attempted given what the parent run showed. NOT NULL because without it the trace is a list, and the finding "this family has been revised 7 times and still fails out of sample" becomes unanswerable.';

CREATE INDEX IF NOT EXISTS experiment_trace_parent_idx ON experiment_trace (parent_run_id);
CREATE INDEX IF NOT EXISTS experiment_trace_child_idx  ON experiment_trace (child_run_id);
CREATE INDEX IF NOT EXISTS experiment_trace_reason_idx ON experiment_trace (reason);

-- ---------------------------------------------------------------------
-- 5. Analyst verdicts -- the loop's return path
--
-- A result is raw measurement. A VERDICT is what the research system concludes
-- from it, and it is the thing that closes the loop: it writes negative
-- knowledge into the Layer 1 failures table, so the next generation of
-- hypotheses is generated in the light of what has already been refuted.
--
-- Without this table the loop is open: results accumulate and nothing reads
-- them.
-- ---------------------------------------------------------------------
DO $$ BEGIN
CREATE TYPE verdict_kind AS ENUM (
    'support',      -- hypothesis stands
    'refute',       -- hypothesis is refuted -> negative knowledge
    'revise',       -- direction right, specifics wrong -> new hypothesis
    'replicate',    -- an external claim survived our conditions
    'inconclusive'  -- the test could not discriminate
);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS analyst_verdicts (
    verdict_id      TEXT PRIMARY KEY,
    run_id          TEXT             NOT NULL REFERENCES experiment_runs(run_id),
    hypothesis_id   TEXT             NOT NULL REFERENCES hypotheses(hypothesis_id),
    kind            verdict_kind     NOT NULL,
    reasoning       TEXT             NOT NULL,
    -- Whether the analyst believes the run was a FAIR test. A supported
    -- verdict from an unfair test is worse than no verdict: it launders a
    -- methodological flaw into evidence.
    test_valid      BOOLEAN          NOT NULL DEFAULT TRUE,
    invalidity_reason TEXT,
    -- What this verdict implies for the mechanism, not just the hypothesis.
    mechanism_verdict TEXT,
    spawned_hypothesis_id TEXT       REFERENCES hypotheses(hypothesis_id),
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT invalid_run_must_explain CHECK (test_valid OR invalidity_reason IS NOT NULL)
);

COMMENT ON TABLE  analyst_verdicts IS
    'The loop''s return path. Turns a measurement into a conclusion, and writes that conclusion into Layer 1 knowledge. Without this the loop is open and results accumulate unread.';
COMMENT ON COLUMN analyst_verdicts.test_valid IS
    'Whether this was a fair test. A supported verdict from an unfair test is worse than no verdict, because it launders a methodological flaw into evidence.';

CREATE INDEX IF NOT EXISTS analyst_verdicts_run_idx  ON analyst_verdicts (run_id);
CREATE INDEX IF NOT EXISTS analyst_verdicts_hyp_idx  ON analyst_verdicts (hypothesis_id);
CREATE INDEX IF NOT EXISTS analyst_verdicts_kind_idx ON analyst_verdicts (kind);

-- ---------------------------------------------------------------------
-- 6. Adapter registry
--
-- Which backends exist and whether they are usable. Recorded rather than
-- hardcoded so an operator can see, at a glance, that the system has never
-- actually run a real engine -- the single most important thing to know
-- before trusting a result.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_backends (
    backend         execution_backend PRIMARY KEY,
    description     TEXT             NOT NULL,
    is_available    BOOLEAN          NOT NULL DEFAULT FALSE,
    is_real_engine  BOOLEAN          NOT NULL DEFAULT FALSE,
    version         TEXT,
    notes           TEXT,
    last_checked_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);

COMMENT ON COLUMN execution_backends.is_real_engine IS
    'False for the stub backend. A stub produces well-formed but meaningless numbers, so a result whose backend is not a real engine must never be presented as evidence.';

INSERT INTO execution_backends (backend, description, is_available, is_real_engine, version, notes)
VALUES
    ('stub', 'Deterministic no-op runner. Produces structurally valid results with no predictive content. Exists so the loop can be tested end to end without a backtest engine.',
     TRUE, FALSE, '1.0.0', 'NOT AN ENGINE. Any result with backend=''stub'' is a plumbing test, never evidence.'),
    ('moondev_rbi', 'External Moon Dev RBI engine, attached behind the adapter port defined in layer2/rbi_adapter.py.',
     FALSE, TRUE, NULL, 'Requires the Moon Dev RBI implementation. Not wired in this repository.'),
    ('external', 'Any third-party engine speaking the ExperimentAdapter protocol.',
     FALSE, TRUE, NULL, 'See layer2/rbi_adapter.py for the required interface.')
ON CONFLICT (backend) DO UPDATE
    SET description = EXCLUDED.description,
        notes = EXCLUDED.notes;

-- ---------------------------------------------------------------------
-- Layer 2 hand-off view.
--
-- One row per run: the claim, the rule, the controlled-experiment metadata,
-- the typed metrics, and the verdict. This is what a human or an analyst
-- reads, and it deliberately does NOT include stdout.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW experiment_run_report AS
SELECT
    r.run_id,
    r.experiment_id,
    r.hypothesis_id,
    r.research_id,
    r.backend,
    r.status,
    r.outcome,
    -- what was tested
    h.statement              AS hypothesis_statement,
    h.expected_mechanism,
    r.dataset, r.dataset_version, r.timeframe, r.time_period, r.universe,
    r.execution_model, r.cost_model, r.parameters, r.random_seed,
    -- reproducibility
    r.code_hash, r.config_hash, r.spec_hash,
    -- typed results (never stdout)
    res.gross_return, res.net_return, res.volatility, res.max_drawdown, res.sharpe,
    res.trade_count, res.turnover, res.hit_rate,
    res.stability, res.regime_dependence, res.p_value, res.multiple_testing_adjusted,
    res.market_exposure, res.residual_alpha, res.mechanism_consistent,
    -- what was concluded
    v.kind AS verdict_kind, v.reasoning AS verdict_reasoning,
    v.test_valid, v.mechanism_verdict,
    -- honesty about the engine
    b.is_real_engine,
    ri.source_ids, ri.model_used, ri.prompt_version
FROM experiment_runs r
LEFT JOIN experiment_results  res ON res.run_id = r.run_id
LEFT JOIN analyst_verdicts   v   ON v.run_id   = r.run_id
LEFT JOIN hypotheses         h   ON h.hypothesis_id = r.hypothesis_id
LEFT JOIN research_identity  ri  ON ri.research_id  = r.research_id
LEFT JOIN execution_backends b   ON b.backend       = r.backend;

COMMENT ON VIEW experiment_run_report IS
    'Everything needed to judge one experiment run in a single row: the claim, the controlled-experiment metadata, the typed metrics, and the verdict. stdout is deliberately absent -- it is retained for forensics and is never the record of what happened.';

COMMIT;