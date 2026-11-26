CREATE TABLE dion_cursors (
    bot_id uuid PRIMARY KEY,
    last_update_id bigint CHECK (last_update_id >= 0)
);

CREATE TABLE dion_feeds (
    bot_id uuid NOT NULL,
    chat_id uuid NOT NULL,
    external_user_id uuid NOT NULL,
    conversation_id uuid NOT NULL UNIQUE REFERENCES conversations(id),
    PRIMARY KEY (bot_id, chat_id)
);

CREATE TABLE dion_updates (
    bot_id uuid NOT NULL,
    update_id bigint NOT NULL CHECK (update_id >= 0),
    kind text NOT NULL CHECK (kind IN ('input', 'message', 'message_failed', 'ignored')),
    chat_id uuid,
    message_id uuid,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    processed_at timestamptz,
    case_id uuid REFERENCES cases(id),
    turn_id uuid,
    FOREIGN KEY (case_id, turn_id) REFERENCES turns(case_id, id),
    PRIMARY KEY (bot_id, update_id)
);
CREATE INDEX dion_updates_pending_idx ON dion_updates (bot_id, kind, update_id)
    WHERE processed_at IS NULL;
CREATE UNIQUE INDEX dion_input_message_unique ON dion_updates (bot_id, chat_id, message_id)
    WHERE kind = 'input';

CREATE TABLE dion_outbox (
    effect_key text PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES dion_feeds(conversation_id),
    case_id uuid,
    turn_id uuid,
    actor_id text NOT NULL,
    text text NOT NULL,
    source_refs jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(source_refs) = 'array'),
    reply_message_id uuid,
    offer_id uuid REFERENCES offers(id),
    clarification_id uuid REFERENCES clarifications(id),
    confirmed_message_id uuid,
    FOREIGN KEY (conversation_id, case_id) REFERENCES cases(conversation_id, id),
    FOREIGN KEY (conversation_id, case_id, turn_id) REFERENCES turns(conversation_id, case_id, id)
);
CREATE INDEX dion_outbox_message_idx ON dion_outbox (conversation_id, confirmed_message_id)
    WHERE confirmed_message_id IS NOT NULL;
