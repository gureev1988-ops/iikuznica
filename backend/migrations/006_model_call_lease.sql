ALTER TABLE model_calls ADD COLUMN lease_token uuid;
ALTER TABLE model_calls ADD CONSTRAINT model_call_job_requires_lease
    CHECK ((job_id IS NULL) = (lease_token IS NULL));
