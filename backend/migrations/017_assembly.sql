CREATE TABLE guest_sessions (
    token_sha256 bytea PRIMARY KEY,
    id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE judge_sessions (
    token_sha256 bytea PRIMARY KEY,
    expires_at timestamptz NOT NULL
);

CREATE TABLE judge_login_limits (
    guest_id uuid PRIMARY KEY REFERENCES guest_sessions(id) ON DELETE CASCADE,
    reset_at timestamptz NOT NULL,
    attempts integer NOT NULL
);

CREATE TABLE assembly_originals (
    revision_id uuid PRIMARY KEY REFERENCES source_revisions(id),
    sha256 text NOT NULL,
    name text NOT NULL,
    path text NOT NULL
);

CREATE TABLE assembly_glossary (
    abbreviation text PRIMARY KEY,
    expansion text NOT NULL
);

CREATE TABLE assembly_postings (
    fragment_id uuid NOT NULL REFERENCES source_fragments(id) ON DELETE CASCADE,
    lexeme text NOT NULL,
    tf integer NOT NULL,
    in_heading boolean NOT NULL,
    PRIMARY KEY (lexeme, fragment_id)
);
CREATE INDEX assembly_postings_fragment ON assembly_postings (fragment_id);

CREATE TABLE assembly_passages (
    fragment_id uuid PRIMARY KEY REFERENCES source_fragments(id) ON DELETE CASCADE,
    doc text NOT NULL,
    ref text NOT NULL,
    section text NOT NULL,
    length integer NOT NULL
);

CREATE TABLE assembly_mail_replies (
    command_id uuid PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES mail_requests(id),
    message_id text NOT NULL UNIQUE,
    reply text NOT NULL,
    in_reply_to text NOT NULL,
    references_ids text[] NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'sent', 'uncertain', 'failed')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE assembly_gaps (
    turn_id uuid PRIMARY KEY REFERENCES turns(id),
    question text NOT NULL,
    conditions jsonb NOT NULL
);
CREATE TABLE assembly_faq_uses (
    turn_id uuid NOT NULL REFERENCES turns(id),
    faq_id uuid NOT NULL REFERENCES faq_entries(id),
    PRIMARY KEY (turn_id, faq_id)
);
