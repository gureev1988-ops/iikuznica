-- One explanation can contradict several independently verified provisions.
CREATE TABLE assembly_quality_conflict_fragments (
    faq_id uuid NOT NULL REFERENCES faq_entries(id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    faq_revision_id uuid NOT NULL REFERENCES source_revisions(id),
    fragment_id uuid NOT NULL REFERENCES source_fragments(id),
    quote text NOT NULL,
    claim text NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY (faq_id, ordinal)
);
-- Older assessments retained only the first piece of evidence.
UPDATE assembly_quality_checks SET catalog='{}' WHERE outcome='conflict';
