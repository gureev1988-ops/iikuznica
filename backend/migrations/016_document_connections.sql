CREATE TABLE document_connections (
    profile text NOT NULL CHECK (profile IN ('integrated', 'hackathon')),
    source_key text NOT NULL,
    interval_seconds numeric CHECK (interval_seconds >= 60 AND scale(interval_seconds) = 0),
    next_due_at timestamptz,
    last_check_at timestamptz,
    last_check_error text,
    PRIMARY KEY (profile, source_key),
    CHECK ((interval_seconds IS NULL) = (next_due_at IS NULL))
);
CREATE INDEX document_connections_due ON document_connections (profile, next_due_at, source_key)
    WHERE next_due_at IS NOT NULL;

CREATE TABLE document_snapshot_versions (
    source_key text PRIMARY KEY,
    snapshot_revision numeric NOT NULL CHECK (snapshot_revision > 0 AND scale(snapshot_revision) = 0),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE document_run_snapshots (
    run_id uuid PRIMARY KEY REFERENCES document_sync_runs(id) ON DELETE CASCADE,
    documents jsonb NOT NULL CHECK (jsonb_typeof(documents) = 'array')
);
CREATE INDEX document_sync_runs_latest ON document_sync_runs (source_key, created_at DESC, id DESC);
CREATE INDEX document_sync_runs_latest_success ON document_sync_runs (source_key, finished_at DESC)
    WHERE status = 'completed';
