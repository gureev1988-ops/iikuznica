CREATE TABLE document_demo_access_cutoffs (
    source_key text NOT NULL,
    external_id text NOT NULL,
    blocked_through_version bigint NOT NULL CHECK (blocked_through_version > 0),
    PRIMARY KEY (source_key, external_id)
);
