-- ОК1 belongs to the published teaching case. Backfill existing cases chronologically.
CREATE SEQUENCE mail_case_number_seq AS bigint START WITH 2;
ALTER TABLE mail_requests ADD COLUMN case_number bigint;
WITH numbered AS (
    SELECT id, 1 + row_number() OVER (ORDER BY created_at, id) AS number FROM mail_requests
)
UPDATE mail_requests r SET case_number = n.number FROM numbered n WHERE n.id = r.id;
SELECT setval('mail_case_number_seq', coalesce(max(case_number), 1) + 1, false) FROM mail_requests;
ALTER TABLE mail_requests ALTER COLUMN case_number SET DEFAULT nextval('mail_case_number_seq');
ALTER TABLE mail_requests ALTER COLUMN case_number SET NOT NULL;
ALTER TABLE mail_requests ADD UNIQUE (case_number), ADD CHECK (case_number > 0);
ALTER SEQUENCE mail_case_number_seq OWNED BY mail_requests.case_number;

-- Persist the interpreted condition independently of the employee's verbatim message.
ALTER TABLE clarifications ADD COLUMN resolved_value text;
