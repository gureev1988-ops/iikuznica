CREATE TABLE mail_requests (
    id uuid PRIMARY KEY,
    offer_id uuid NOT NULL UNIQUE REFERENCES offers(id),
    conversation_id uuid NOT NULL,
    case_id uuid NOT NULL,
    turn_id uuid NOT NULL,
    actor_id text NOT NULL,
    actor_email text NOT NULL,
    channel text NOT NULL,
    question text NOT NULL,
    conditions jsonb NOT NULL,
    recipient text NOT NULL,
    sender text NOT NULL,
    reply_to text NOT NULL,
    token text NOT NULL UNIQUE,
    state text NOT NULL,
    pending jsonb NOT NULL DEFAULT '[]',
    last_inbound_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id, case_id, turn_id) REFERENCES turns(conversation_id, case_id, id)
);

CREATE TABLE mail_cursors (
    mailbox text PRIMARY KEY,
    uidvalidity text NOT NULL,
    last_uid bigint NOT NULL CHECK (last_uid >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE mail_inbound (
    id uuid PRIMARY KEY,
    mailbox text NOT NULL,
    uidvalidity text NOT NULL,
    uid bigint NOT NULL CHECK (uid > 0),
    fingerprint text NOT NULL,
    message_id text,
    trusted boolean NOT NULL,
    raw bytea NOT NULL,
    parsed jsonb NOT NULL,
    request_id uuid REFERENCES mail_requests(id),
    state text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (mailbox, uidvalidity, uid),
    UNIQUE (mailbox, fingerprint)
);
CREATE INDEX mail_inbound_thread_idx ON mail_inbound(message_id) WHERE request_id IS NOT NULL AND trusted;
CREATE UNIQUE INDEX mail_inbound_identity_idx ON mail_inbound(mailbox, message_id)
    WHERE trusted AND message_id IS NOT NULL;
ALTER TABLE mail_requests ADD FOREIGN KEY (last_inbound_id) REFERENCES mail_inbound(id);

CREATE TABLE mail_outbound (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES mail_requests(id),
    business_key text NOT NULL,
    message_id text NOT NULL UNIQUE,
    in_reply_to text,
    references_ids jsonb NOT NULL DEFAULT '[]',
    body text NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    attempt_id uuid REFERENCES delivery_attempts(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_id, business_key)
);

CREATE TABLE faq_entries (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL UNIQUE REFERENCES mail_requests(id),
    question_key text NOT NULL,
    conditions_key text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('active', 'withdrawn')),
    document_id uuid NOT NULL UNIQUE REFERENCES sources(id),
    current_revision_id uuid NOT NULL REFERENCES source_revisions(id),
    current_fragment_id uuid NOT NULL REFERENCES source_fragments(id),
    covered_revision_id uuid REFERENCES source_revisions(id),
    conflict_revision_id uuid REFERENCES source_revisions(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX faq_match_idx ON faq_entries(question_key, conditions_key) WHERE status = 'active';
CREATE INDEX faq_covered_revision_idx ON faq_entries(covered_revision_id);
CREATE INDEX faq_conflict_revision_idx ON faq_entries(conflict_revision_id);
