-- The previous A1 formatter included the alternative FAQ in the citations on a
-- regulation-first answer and offered to confirm the discrepancy. Rebuild only
-- these derived counts from that exact historical output; the turn is preserved.
WITH alternative AS (
    SELECT t.id AS turn_id, f.id AS faq_id FROM turns t
    CROSS JOIN LATERAL jsonb_array_elements(t.result->'sources') ref
    JOIN faq_entries f ON f.document_id::text=ref->>'document_id'
    WHERE ref->>'kind'='faq' AND t.result->>'action'='answer'
      AND t.result->>'text' LIKE E'Регламент\n%\n\nFAQ Отдела качества\n%\n\nРуководствуйтесь регламентом. Повторно уточнить расхождение письмом?'
), corrected AS (
    DELETE FROM assembly_faq_uses u USING alternative a
    WHERE u.turn_id=a.turn_id AND u.faq_id=a.faq_id
)
DELETE FROM assembly_gaps g USING turns t
WHERE g.turn_id=t.id AND EXISTS(SELECT 1 FROM alternative a WHERE a.turn_id=t.id)
  AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(t.result->'parts') p WHERE p->>'action'='escalate');

-- A gap's evidence is independent of citations answering other parts of a question.
CREATE TABLE assembly_gap_evidence (
    turn_id uuid NOT NULL REFERENCES assembly_gaps(turn_id),
    fragment_id uuid NOT NULL REFERENCES source_fragments(id),
    PRIMARY KEY(turn_id,fragment_id)
);
INSERT INTO assembly_gap_evidence
SELECT DISTINCT g.turn_id, f.id FROM assembly_gaps g
JOIN turns t ON t.id=g.turn_id
CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN t.result->>'action'='escalate'
    AND jsonb_array_length(COALESCE(t.result->'parts','[]'))=0 THEN t.result->'sources'
    ELSE (SELECT jsonb_agg(ref) FROM jsonb_array_elements(t.result->'parts') p,
          jsonb_array_elements(p->'sources') ref WHERE p->>'action'='escalate') END) src
JOIN source_fragments f ON f.id::text=src->>'fragment_id'
WHERE src->>'kind'='document';

CREATE TABLE assembly_message_ratings (
    message_id uuid PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    rating text NOT NULL CHECK (rating IN ('up', 'down'))
);

CREATE TABLE assembly_gap_groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic text NOT NULL,
    conditions jsonb NOT NULL,
    example text NOT NULL,
    search_text tsvector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX assembly_gap_groups_search ON assembly_gap_groups USING gin(search_text);

CREATE TABLE assembly_gap_members (
    case_id uuid PRIMARY KEY REFERENCES cases(id),
    group_id uuid NOT NULL REFERENCES assembly_gap_groups(id),
    turn_id uuid NOT NULL UNIQUE REFERENCES assembly_gaps(turn_id)
);
CREATE INDEX assembly_gap_members_group ON assembly_gap_members(group_id);

-- Existing unanswered cases are eligible for the same asynchronous classifier.
INSERT INTO jobs(id, queue, kind, business_key, payload, max_attempts)
SELECT gen_random_uuid(), 'background', 'assembly.group-gap', case_id::text,
       jsonb_build_object('case_id',case_id), 8
FROM (
    SELECT DISTINCT t.case_id FROM assembly_gaps g JOIN turns t ON t.id=g.turn_id
    WHERE t.updated_at >= ((now() AT TIME ZONE 'Europe/Moscow')::date-29)::timestamp
                              AT TIME ZONE 'Europe/Moscow'
) prior;
