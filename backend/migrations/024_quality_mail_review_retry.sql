ALTER TABLE assembly_quality_checks
    ADD COLUMN pending_message_id uuid REFERENCES messages(id) ON DELETE SET NULL;

-- Older checks recorded a transport failure against the current catalog as a final outcome.
-- Only the judge's specific failure reasons need retrying without another import.
UPDATE assembly_quality_checks AS ck
SET retry_after=CASE WHEN ck.retry_after IS NULL AND ck.outcome='unconfirmed' AND ck.reason IN (
        'Судья недоступен. Повторите проверку.',
        'Судья не вернул подтверждаемого результата. Повторите проверку.',
        'Назначена проверка действующего регламента.'
    ) THEN now() ELSE ck.retry_after END,
    pending_message_id=(
        SELECT m.id FROM faq_entries f JOIN mail_requests r ON r.id=f.request_id
        JOIN messages m ON m.conversation_id=r.conversation_id AND m.case_id=r.case_id
        WHERE f.id=ck.faq_id AND r.channel='guest' AND m.role='assistant'
          AND m.text LIKE E'Разъяснение Отдела качества\n%'
          AND jsonb_array_length(m.source_refs)=1
          AND m.source_refs->0->>'revision_id'=ck.faq_revision_id::text
        ORDER BY m.created_at DESC,m.id LIMIT 1
    )
WHERE ck.outcome IN ('unconfirmed','no_source');
