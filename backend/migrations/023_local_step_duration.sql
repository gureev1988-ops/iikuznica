ALTER TABLE assembly_local_steps ADD COLUMN elapsed_ms double precision CHECK (elapsed_ms >= 0);
