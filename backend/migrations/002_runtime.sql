CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    queue text NOT NULL CHECK (queue IN ('interactive', 'background')),
    kind text NOT NULL,
    business_key text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deadline_at timestamptz,
    lease_token uuid,
    lease_expires_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    first_started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (queue, kind, business_key),
    CHECK ((state = 'running') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE INDEX jobs_claim_idx ON jobs (queue, available_at, created_at) WHERE state = 'queued';
CREATE INDEX jobs_lease_idx ON jobs (queue, lease_expires_at) WHERE state = 'running';

CREATE TABLE delivery_attempts (
    id uuid PRIMARY KEY,
    channel text NOT NULL CHECK (channel IN ('smtp', 'dion')),
    effect_key text NOT NULL,
    job_id uuid REFERENCES jobs (id),
    lease_token uuid,
    state text NOT NULL CHECK (state IN ('sending', 'sent', 'failed', 'delivery_unknown')),
    provider_id text,
    detail text,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((job_id IS NULL) = (lease_token IS NULL))
);

CREATE UNIQUE INDEX delivery_effect_in_flight ON delivery_attempts (channel, effect_key)
    WHERE state <> 'failed';
CREATE INDEX delivery_attempts_expiry_idx ON delivery_attempts (expires_at) WHERE state = 'sending';
CREATE INDEX delivery_provider_idx ON delivery_attempts (channel, provider_id) WHERE provider_id IS NOT NULL;

CREATE TABLE model_calls (
    id uuid PRIMARY KEY,
    job_id uuid REFERENCES jobs (id),
    turn_id uuid,
    case_id uuid,
    role text NOT NULL,
    trace_id text NOT NULL,
    model text NOT NULL,
    route text NOT NULL,
    state text NOT NULL CHECK (state IN ('calling', 'succeeded', 'failed', 'outcome_unknown')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    finished_at timestamptz,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    cost numeric CHECK (cost >= 0),
    detail text
);

CREATE INDEX model_calls_turn_idx ON model_calls (turn_id);
CREATE INDEX model_calls_case_idx ON model_calls (case_id);
CREATE INDEX model_calls_expiry_idx ON model_calls (expires_at) WHERE state = 'calling';
