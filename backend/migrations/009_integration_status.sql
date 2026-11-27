CREATE TABLE integration_status (
    name text PRIMARY KEY,
    state text NOT NULL,
    checked_at timestamptz NOT NULL,
    error_kind text
);
