ALTER TABLE messages ADD COLUMN source_refs jsonb
    CHECK (jsonb_typeof(source_refs) = 'array');
