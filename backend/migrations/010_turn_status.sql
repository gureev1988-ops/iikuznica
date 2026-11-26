ALTER TABLE turns ADD CONSTRAINT turns_status_valid
    CHECK (status IN ('queued', 'processing', 'completed', 'technical_failure'));
