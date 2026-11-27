CREATE TABLE assembly_opened_citations (
    turn_id uuid NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    fragment_id uuid NOT NULL REFERENCES source_fragments(id) ON DELETE CASCADE,
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (turn_id, fragment_id)
);

CREATE TABLE assembly_local_steps (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id uuid NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    role text NOT NULL,
    started_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('succeeded', 'failed'))
);
CREATE INDEX assembly_local_steps_turn ON assembly_local_steps(turn_id);
CREATE INDEX assembly_latest_turn ON turns(conversation_id, created_at DESC, id DESC);
