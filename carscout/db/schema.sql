-- CarScout schema. Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS platforms (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL DEFAULT '',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform        TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'all',
    run_date        DATE NOT NULL DEFAULT CURRENT_DATE,
    mode            TEXT NOT NULL DEFAULT 'incremental',   -- incremental | rerun | resume
    status          TEXT NOT NULL DEFAULT 'running',        -- running | finished | failed | aborted
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    parent_run_id   UUID REFERENCES crawl_runs(id),
    cursor_before   JSONB,
    cursor_after    JSONB,
    stats           JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS crawl_runs_platform_started_idx ON crawl_runs (platform, started_at DESC);

CREATE TABLE IF NOT EXISTS crawl_cursors (
    platform        TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'all',
    cursor          JSONB NOT NULL DEFAULT '{}'::jsonb,
    completed       BOOLEAN NOT NULL DEFAULT FALSE,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    consecutive_ok_pages INTEGER NOT NULL DEFAULT 0,
    last_error          JSONB,
    stopped_reason      TEXT,
    run_id          UUID REFERENCES crawl_runs(id),
    run_date        DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, scope)
);

CREATE TABLE IF NOT EXISTS platform_health (
    platform            TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_state       TEXT NOT NULL DEFAULT 'closed',   -- closed | open | half_open
    opened_at           TIMESTAMPTZ,
    cooldown_until      TIMESTAMPTZ,
    last_error_category TEXT,
    last_error_detail   TEXT,
    last_success_at     TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vehicles (
    id                  BIGSERIAL PRIMARY KEY,
    brand               TEXT,
    series              TEXT,
    reg_year            INTEGER,
    reg_month           INTEGER,
    mileage_km          INTEGER,
    displacement_ml     INTEGER,
    transmission        TEXT,
    transfer_count      INTEGER,
    city                TEXT,
    seller_type         TEXT,
    win_vin             TEXT,
    vin_tail6           TEXT,
    title_rep           TEXT,
    description_rep     TEXT,
    image_phash         BIT(64),
    first_listed_at     TIMESTAMPTZ,
    last_seen_at        TIMESTAMPTZ,
    platform_count      INTEGER NOT NULL DEFAULT 0,
    merged_confidence   NUMERIC(5,4) NOT NULL DEFAULT 1.0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vehicles_series_idx ON vehicles (brand, series, reg_year, reg_month, city);
CREATE INDEX IF NOT EXISTS vehicles_vin_tail_idx ON vehicles (vin_tail6);

CREATE TABLE IF NOT EXISTS listings (
    id                  BIGSERIAL PRIMARY KEY,
    platform            TEXT NOT NULL,
    platform_listing_id TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    url_norm            TEXT NOT NULL,
    title               TEXT,
    brand               TEXT,
    series              TEXT,
    reg_year            INTEGER,
    reg_month           INTEGER,
    mileage_km          INTEGER,
    displacement_ml     INTEGER,
    transmission        TEXT,
    transfer_count      INTEGER,
    city                TEXT,
    price_yuan          BIGINT,
    listed_at           TIMESTAMPTZ,
    seller_type         TEXT,
    description         TEXT,
    vin                 TEXT,
    vin_tail6           TEXT,
    image_urls          JSONB NOT NULL DEFAULT '[]'::jsonb,
    image_phash         BIT(64),
    content_hash        TEXT,
    detail_status       TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | schema_drift | blocked | missing | skipped_unchanged
    list_seen_at        TIMESTAMPTZ,
    detail_fetched_at   TIMESTAMPTZ,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    removed_at          TIMESTAMPTZ,
    price_observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    vehicle_id          BIGINT REFERENCES vehicles(id),
    match_status        TEXT NOT NULL DEFAULT 'unmatched', -- unmatched | auto | manual_pending | manual_confirmed | ruled | rejected | pending_rereview
    match_score         NUMERIC(5,4),
    raw_list            JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    extra               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, platform_listing_id)
);
CREATE INDEX IF NOT EXISTS listings_vehicle_idx ON listings (vehicle_id);
CREATE INDEX IF NOT EXISTS listings_match_status_idx ON listings (match_status);
CREATE INDEX IF NOT EXISTS listings_vin_tail_idx ON listings (vin_tail6);
CREATE INDEX IF NOT EXISTS listings_recall_idx ON listings (brand, series, reg_year, reg_month, city);

CREATE TABLE IF NOT EXISTS listing_price_events (
    id              BIGSERIAL PRIMARY KEY,
    listing_id      BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price_yuan      BIGINT NOT NULL,
    previous_price  BIGINT,
    delta_yuan      BIGINT,
    run_id          UUID REFERENCES crawl_runs(id),
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (listing_id, observed_at, price_yuan)
);
CREATE INDEX IF NOT EXISTS price_events_listing_idx ON listing_price_events (listing_id, observed_at);

CREATE TABLE IF NOT EXISTS image_hashes (
    id              BIGSERIAL PRIMARY KEY,
    listing_id      BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    image_url       TEXT NOT NULL,
    phash           BIT(64) NOT NULL,
    width           INTEGER,
    height          INTEGER,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (listing_id, image_url)
);
CREATE INDEX IF NOT EXISTS image_hashes_listing_idx ON image_hashes (listing_id);

CREATE TABLE IF NOT EXISTS match_candidates (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID REFERENCES crawl_runs(id),
    listing_a       BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    listing_b       BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    score           NUMERIC(5,4) NOT NULL,
    components      JSONB NOT NULL DEFAULT '{}'::jsonb,
    state           TEXT NOT NULL DEFAULT 'pending',  -- auto_merged | manual_pending | rejected | rule_applied
    rule_id         BIGINT,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    UNIQUE (listing_a, listing_b)
);
CREATE INDEX IF NOT EXISTS match_candidates_state_idx ON match_candidates (state);

CREATE TABLE IF NOT EXISTS manual_review_queue (
    id              BIGSERIAL PRIMARY KEY,
    candidate_id    BIGINT NOT NULL REFERENCES match_candidates(id) ON DELETE CASCADE,
    batch_id        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed_merge | confirmed_split
    reviewer        TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at     TIMESTAMPTZ,
    UNIQUE (candidate_id, batch_id)
);
CREATE INDEX IF NOT EXISTS review_queue_status_idx ON manual_review_queue (status, created_at);

CREATE TABLE IF NOT EXISTS match_rules (
    id              BIGSERIAL PRIMARY KEY,
    rule_key        TEXT NOT NULL UNIQUE,
    decision        TEXT NOT NULL,                     -- merge | split
    source          TEXT NOT NULL DEFAULT 'manual',
    confidence      NUMERIC(5,4) NOT NULL DEFAULT 0.95,
    note            TEXT,
    applied_count   INTEGER NOT NULL DEFAULT 0,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_archive (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID REFERENCES crawl_runs(id),
    platform        TEXT NOT NULL,
    url             TEXT NOT NULL,
    url_norm        TEXT NOT NULL,
    http_status     INTEGER,
    kind            TEXT NOT NULL,        -- list | detail | xhr | anomaly
    category        TEXT,                 -- ok | empty_confirmed | blocked | schema_drift | http_error | signature_expired
    content_type    TEXT,
    path            TEXT NOT NULL,
    size_bytes      BIGINT,
    sha256          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS archive_run_idx ON ingest_archive (run_id, created_at);

CREATE TABLE IF NOT EXISTS ingest_anomalies (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID REFERENCES crawl_runs(id),
    platform        TEXT NOT NULL,
    url             TEXT,
    http_status     INTEGER,
    category        TEXT NOT NULL,        -- empty_blocked | blocked_signal | schema_drift | http_error | signature_expired | parser_error
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    archive_id      BIGINT REFERENCES ingest_archive(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS anomalies_platform_idx ON ingest_anomalies (platform, created_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 归并规则版本化：阈值 / 字段权重 / 必要证据 / 灰度放量 / 离线回放
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_rule_versions (
    id                  BIGSERIAL PRIMARY KEY,
    version             INTEGER NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',      -- draft | active | inactive | rollback（回放审批在 merge_rule_replays.status）
    spec                JSONB NOT NULL,                     -- 完整规则参数（阈值/权重/必要证据/闸门/召回）
    changelog           TEXT,
    created_by          TEXT NOT NULL DEFAULT 'operator',
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    activated_at        TIMESTAMPTZ,
    rolled_back_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS merge_rule_versions_status_idx ON merge_rule_versions (status, version);

CREATE TABLE IF NOT EXISTS merge_rule_rollouts (
    id                  BIGSERIAL PRIMARY KEY,
    rule_version_id     BIGINT NOT NULL REFERENCES merge_rule_versions(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,                      -- A/B/C/D；'*' 表示全平台兜底
    rollout_pct         INTEGER NOT NULL CHECK (rollout_pct BETWEEN 0 AND 100),
    updated_by          TEXT NOT NULL DEFAULT 'operator',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, platform)
);
CREATE INDEX IF NOT EXISTS merge_rule_rollouts_version_idx ON merge_rule_rollouts (rule_version_id);

CREATE TABLE IF NOT EXISTS merge_rule_replays (
    id                  BIGSERIAL PRIMARY KEY,
    rule_version_id     BIGINT NOT NULL REFERENCES merge_rule_versions(id) ON DELETE CASCADE,
    baseline_version_id BIGINT REFERENCES merge_rule_versions(id),
    status              TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | rejected
    pairs_evaluated     INTEGER NOT NULL DEFAULT 0,
    extra_merges        INTEGER NOT NULL DEFAULT 0,         -- 新增自动合并的配对数
    split_confirmed     INTEGER NOT NULL DEFAULT 0,         -- 会拆散的已人工确认组合数
    extra_gray          INTEGER NOT NULL DEFAULT 0,         -- 原自动合并 -> 灰区
    extra_rejects       INTEGER NOT NULL DEFAULT 0,         -- 原灰区/合并 -> 直接拒绝
    unchanged           INTEGER NOT NULL DEFAULT 0,
    approval_threshold  JSONB NOT NULL DEFAULT '{}'::jsonb,
    exceeds_threshold   BOOLEAN NOT NULL DEFAULT FALSE,
    report              JSONB NOT NULL DEFAULT '{}'::jsonb,-- 明细样本（证据级差异）
    requested_by        TEXT NOT NULL DEFAULT 'operator',
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS merge_rule_replays_version_idx ON merge_rule_replays (rule_version_id, id DESC);

-- ---------------------------------------------------------------------------
-- Idempotent migrations (CREATE TABLE IF NOT EXISTS does not add columns)
-- ---------------------------------------------------------------------------
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;
ALTER TABLE crawl_cursors ADD COLUMN IF NOT EXISTS last_error JSONB;
ALTER TABLE crawl_cursors ADD COLUMN IF NOT EXISTS stopped_reason TEXT;
-- 规则版本戳：每条挂牌/候选按哪个规则版本判定；回滚后新规则合并的记录转 pending_rereview
ALTER TABLE listings ADD COLUMN IF NOT EXISTS rule_version_id BIGINT REFERENCES merge_rule_versions(id);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS pending_rereview_reason TEXT;
ALTER TABLE match_candidates ADD COLUMN IF NOT EXISTS rule_version_id BIGINT REFERENCES merge_rule_versions(id);
ALTER TABLE match_candidates ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS rule_version_id BIGINT REFERENCES merge_rule_versions(id);
CREATE INDEX IF NOT EXISTS listings_active_idx ON listings (platform, is_active, last_seen_at);
CREATE INDEX IF NOT EXISTS listings_rule_version_idx ON listings (rule_version_id);
CREATE INDEX IF NOT EXISTS listings_pending_rereview_idx ON listings (match_status)
    WHERE match_status = 'pending_rereview';
