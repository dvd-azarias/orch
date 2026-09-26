CREATE TABLE IF NOT EXISTS orch_journey_workspace_snapshot_state (
    singleton_id SMALLINT PRIMARY KEY DEFAULT 1,
    timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    dirty_generation BIGINT NOT NULL DEFAULT 0,
    built_generation BIGINT NOT NULL DEFAULT 0,
    snapshot_sequence BIGINT NOT NULL DEFAULT 0,
    snapshot_id UUID,
    snapshot_payload JSONB,
    snapshot_bytes INTEGER,
    snapshot_sha256 TEXT,
    dirty_since TIMESTAMPTZ,
    latest_dirty_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claim_token UUID,
    claim_generation BIGINT,
    claimed_at TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,
    last_built_at TIMESTAMPTZ,
    last_notification_at TIMESTAMPTZ,
    last_error_code TEXT,
    last_error_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_singleton
        CHECK (singleton_id = 1),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_sequence
        CHECK (snapshot_sequence >= 0),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_generation
        CHECK (
            dirty_generation >= 0
            AND built_generation >= 0
            AND built_generation <= dirty_generation
        ),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_attempt_count
        CHECK (attempt_count >= 0),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_payload
        CHECK (snapshot_payload IS NULL OR jsonb_typeof(snapshot_payload) = 'object'),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_bytes
        CHECK (snapshot_bytes IS NULL OR snapshot_bytes BETWEEN 0 AND 1048576),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_payload_pair
        CHECK ((snapshot_payload IS NULL) = (snapshot_bytes IS NULL)),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_hash_pair
        CHECK ((snapshot_payload IS NULL) = (snapshot_sha256 IS NULL)),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_hash
        CHECK (snapshot_sha256 IS NULL OR snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_snapshot_pair
        CHECK ((snapshot_payload IS NULL) = (snapshot_id IS NULL)),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_snapshot_sequence
        CHECK ((snapshot_sequence = 0) = (snapshot_payload IS NULL)),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_dirty_pair
        CHECK ((dirty_since IS NULL) = (latest_dirty_at IS NULL)),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_dirty_order
        CHECK (latest_dirty_at IS NULL OR latest_dirty_at >= dirty_since),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_clean_generation
        CHECK (dirty_since IS NOT NULL OR built_generation = dirty_generation),
    CONSTRAINT chk_orch_journey_workspace_snapshot_state_claim
        CHECK (
            (
                claim_token IS NULL
                AND claim_generation IS NULL
                AND claimed_at IS NULL
                AND claim_expires_at IS NULL
            )
            OR (
                claim_token IS NOT NULL
                AND claim_generation IS NOT NULL
                AND claim_generation > built_generation
                AND claim_generation <= dirty_generation
                AND claimed_at IS NOT NULL
                AND claim_expires_at IS NOT NULL
                AND claim_expires_at >= claimed_at
            )
        )
);

INSERT INTO orch_journey_workspace_snapshot_state (singleton_id)
VALUES (1)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_orch_journey_workspace_snapshot_state_due
    ON orch_journey_workspace_snapshot_state (next_attempt_at, dirty_since)
    WHERE dirty_since IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_orch_journey_workspace_snapshot_state_lease
    ON orch_journey_workspace_snapshot_state (claim_expires_at)
    WHERE claim_token IS NOT NULL;
