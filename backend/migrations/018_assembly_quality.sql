-- Public demo threads are scoped to the assembly mailbox; the original mail is immutable.
CREATE TABLE assembly_quality_seed (
    faq_id uuid PRIMARY KEY REFERENCES faq_entries(id)
);
CREATE TABLE assembly_quality_checks (
    faq_id uuid PRIMARY KEY REFERENCES faq_entries(id),
    faq_revision_id uuid NOT NULL REFERENCES source_revisions(id),
    outcome text NOT NULL CHECK (outcome IN ('conflict','consistent','unconfirmed','no_source')),
    fragment_id uuid REFERENCES source_fragments(id),
    catalog uuid[] NOT NULL,
    quote text,
    reason text NOT NULL
);
CREATE TABLE assembly_quality_history (
    faq_id uuid NOT NULL REFERENCES faq_entries(id),
    version integer NOT NULL,
    question text NOT NULL,
    answer text NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (faq_id,version)
);
CREATE TABLE assembly_quality_reports (
    id uuid PRIMARY KEY,
    subject text NOT NULL,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivery text NOT NULL DEFAULT 'pending'
);
ALTER TABLE faq_entries ADD COLUMN display_question text;
ALTER TABLE judge_sessions ADD COLUMN demo_only boolean NOT NULL DEFAULT false;
ALTER TABLE assembly_quality_checks ADD COLUMN retry_after timestamptz;
