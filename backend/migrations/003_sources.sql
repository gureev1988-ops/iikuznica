CREATE TABLE sources (
    id uuid PRIMARY KEY,
    source_system text NOT NULL CHECK (source_system IN ('document', 'faq')),
    source_key text NOT NULL,
    external_id text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'removed', 'archived', 'access_lost', 'moved')),
    space_key text,
    acl_actor_ids text[],
    current_revision_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_key, external_id),
    CHECK (status <> 'active' OR current_revision_id IS NOT NULL)
);
CREATE TABLE source_revisions (
    id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES sources(id),
    version bigint NOT NULL CHECK (version > 0),
    title text NOT NULL,
    storage_body text NOT NULL DEFAULT '',
    checksum text NOT NULL,
    upstream_url text,
    space_key text,
    acl_actor_ids text[],
    indexed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, version),
    UNIQUE (source_id, id)
);
ALTER TABLE sources ADD CONSTRAINT source_current_revision_matches_source
    FOREIGN KEY (id, current_revision_id) REFERENCES source_revisions(source_id, id);
CREATE TABLE source_fragments (
    id uuid PRIMARY KEY,
    revision_id uuid NOT NULL REFERENCES source_revisions(id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    anchor text NOT NULL,
    heading_path text[] NOT NULL DEFAULT '{}',
    text text NOT NULL,
    UNIQUE (revision_id, id),
    UNIQUE (revision_id, ordinal),
    UNIQUE (revision_id, anchor)
);

CREATE TABLE document_sync_runs (
    id uuid PRIMARY KEY,
    source_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'incomplete', 'completed')),
    cursor text,
    batch_offset integer NOT NULL DEFAULT 0 CHECK (batch_offset >= 0),
    listing_done boolean NOT NULL DEFAULT false,
    error text,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX document_sync_one_pending ON document_sync_runs (source_key)
    WHERE status IN ('queued', 'running', 'incomplete');
CREATE TABLE document_sync_seen (
    run_id uuid NOT NULL REFERENCES document_sync_runs(id) ON DELETE CASCADE,
    external_id text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('active', 'removed', 'archived', 'access_lost', 'moved')),
    PRIMARY KEY (run_id, external_id)
);
