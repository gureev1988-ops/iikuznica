ALTER TABLE delivery_attempts ADD CONSTRAINT delivery_attempt_id_effect_key_unique
    UNIQUE (id, effect_key);

CREATE TABLE dion_dispatches (
    attempt_id uuid PRIMARY KEY,
    effect_key text NOT NULL REFERENCES dion_outbox(effect_key),
    envelope jsonb NOT NULL,
    rendered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (attempt_id, effect_key) REFERENCES delivery_attempts(id, effect_key),
    CHECK (
        jsonb_typeof(envelope) = 'object'
        AND envelope ?& ARRAY[
            'text', 'source_refs', 'substitute', 'offer_id', 'clarification_id',
            'mail_request_id', 'reply_message_id'
        ]
        AND jsonb_typeof(envelope->'text') = 'string'
        AND jsonb_typeof(envelope->'source_refs') = 'array'
        AND jsonb_typeof(envelope->'substitute') = 'boolean'
    )
);
CREATE INDEX dion_dispatch_effect_idx ON dion_dispatches (effect_key);

CREATE TABLE dion_attempt_outcomes (
    attempt_id uuid NOT NULL REFERENCES delivery_attempts(id),
    state text NOT NULL CHECK (state IN ('delivery_unknown', 'sent', 'failed')),
    provider_id text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (attempt_id, state)
);

-- Only the attempt-keyed events carry the actual rendered content and audit states.
WITH historical_events AS (
    SELECT a.id AS attempt_id, a.effect_key, e.kind, e.payload, e.created_at, e.cursor
    FROM delivery_attempts a
    JOIN dion_outbox o ON o.effect_key = a.effect_key
    JOIN events e
        ON e.conversation_id = o.conversation_id
        AND e.case_id = o.case_id
        AND e.turn_id IS NOT DISTINCT FROM o.turn_id
        AND e.payload->>'attempt_id' = a.id::text
    WHERE a.channel = 'dion' AND e.kind IN (
        'dion.dispatch_prepared', 'dion.delivery_unknown', 'dion.delivery_sent', 'dion.delivery_failed'
    )
), dispatches AS (
    INSERT INTO dion_dispatches (attempt_id, effect_key, envelope, rendered_at)
    SELECT DISTINCT ON (attempt_id) attempt_id, effect_key, payload - 'attempt_id', created_at
    FROM historical_events
    WHERE kind = 'dion.dispatch_prepared'
        AND payload ?& ARRAY[
            'text', 'source_refs', 'substitute', 'offer_id', 'clarification_id',
            'mail_request_id', 'reply_message_id'
        ]
        AND jsonb_typeof(payload->'text') = 'string'
        AND jsonb_typeof(payload->'source_refs') = 'array'
        AND jsonb_typeof(payload->'substitute') = 'boolean'
    ORDER BY attempt_id, cursor
)
INSERT INTO dion_attempt_outcomes (attempt_id, state, provider_id, recorded_at)
SELECT DISTINCT ON (attempt_id, payload->>'state')
    attempt_id, payload->>'state', payload->>'provider_id', created_at
FROM historical_events
WHERE (kind, payload->>'state') IN (
    ('dion.delivery_unknown', 'delivery_unknown'),
    ('dion.delivery_sent', 'sent'),
    ('dion.delivery_failed', 'failed')
)
ORDER BY attempt_id, payload->>'state', cursor;
