ALTER TABLE cases ADD COLUMN condition_sources jsonb NOT NULL DEFAULT '{}'
    CHECK (jsonb_typeof(condition_sources) = 'object');
