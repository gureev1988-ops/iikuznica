ALTER TABLE faq_entries ADD COLUMN coverage_source_id uuid REFERENCES sources(id);
CREATE INDEX faq_coverage_source_idx ON faq_entries (coverage_source_id);
