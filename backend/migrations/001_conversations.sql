CREATE TABLE conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    event_cursor bigint NOT NULL DEFAULT 0,
    events_floor bigint NOT NULL DEFAULT 0,
    CHECK (events_floor >= 0 AND events_floor <= event_cursor)
);

CREATE TABLE cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    conditions jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(conditions) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, id)
);

CREATE TABLE turns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    case_id uuid NOT NULL,
    request_id text NOT NULL,
    question text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    request jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(request) = 'object'),
    result jsonb CHECK (jsonb_typeof(result) = 'object'),
    error text,
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id, case_id) REFERENCES cases(conversation_id, id),
    UNIQUE (conversation_id, request_id),
    UNIQUE (conversation_id, case_id, id),
    UNIQUE (case_id, id)
);

CREATE TABLE messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    case_id uuid NOT NULL,
    turn_id uuid,
    client_message_id text,
    role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id, case_id) REFERENCES cases(conversation_id, id),
    FOREIGN KEY (conversation_id, case_id, turn_id) REFERENCES turns(conversation_id, case_id, id),
    UNIQUE (conversation_id, client_message_id)
);
CREATE INDEX messages_conversation_history_idx ON messages (conversation_id, created_at, id);
CREATE INDEX messages_case_history_idx ON messages (case_id, created_at, id);

CREATE TABLE clarifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id uuid NOT NULL,
    case_id uuid NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'superseded')),
    prompt text NOT NULL,
    options jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(options) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (case_id, turn_id) REFERENCES turns(case_id, id),
    UNIQUE (turn_id)
);

CREATE TABLE offers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id uuid NOT NULL,
    case_id uuid NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'declined', 'confirmed', 'superseded')),
    text text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (case_id, turn_id) REFERENCES turns(case_id, id),
    UNIQUE (turn_id)
);
CREATE INDEX offers_open_for_case_idx ON offers (case_id) WHERE status = 'open';

CREATE TABLE events (
    conversation_id uuid NOT NULL,
    cursor bigint NOT NULL CHECK (cursor > 0),
    case_id uuid NOT NULL,
    turn_id uuid,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id, case_id) REFERENCES cases(conversation_id, id),
    FOREIGN KEY (conversation_id, case_id, turn_id) REFERENCES turns(conversation_id, case_id, id),
    PRIMARY KEY (conversation_id, cursor)
);
