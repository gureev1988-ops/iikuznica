ALTER TABLE dion_outbox ADD COLUMN mail_request_id uuid REFERENCES mail_requests(id);
