"""Employee-visible metrics derived from completed dialogue and quality decisions."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from backend.runtime import enqueue


GROUP_JOB = "assembly.group-gap"
MOSCOW = ZoneInfo("Europe/Moscow")

# The same current document ACL / historical revision ACL as documents.catalog.get_fragment.
VISIBLE_DOCUMENT = """
    s.source_system='document' AND s.status='active' AND r.indexed_at IS NOT NULL
    AND (s.acl_actor_ids IS NULL OR %(actor)s=ANY(s.acl_actor_ids))
    AND (s.current_revision_id=r.id OR r.acl_actor_ids IS NULL OR %(actor)s=ANY(r.acl_actor_ids))
"""


def record_facts(conn, turn, public, conditions, target):
    """Only public, checked evidence can become a citation or a gap attribution."""
    alternative = any(ref.get("claim") == "Альтернативное разъяснение"
                      for ref in public["sources"])
    unresolved_part = any(part["action"] == "escalate" for part in public.get("parts", []))
    if target and (not alternative or unresolved_part):
        conn.execute("""INSERT INTO assembly_gaps(turn_id,question,conditions)
                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                     (turn["id"], target, Jsonb(conditions)))
        unresolved_refs = [ref for part in public.get("parts", []) if part["action"] == "escalate"
                           for ref in part["sources"]]
        if public["action"] == "escalate" and not public.get("parts"):
            unresolved_refs = public["sources"]
        for ref in unresolved_refs:
            if ref["kind"] == "document":
                conn.execute("""INSERT INTO assembly_gap_evidence VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                             (turn["id"], ref["fragment_id"]))
        enqueue(conn, queue="background", kind=GROUP_JOB, key=str(turn["case_id"]),
                payload={"case_id": str(turn["case_id"])}, max_attempts=8)

    refs = list(public["sources"]) if public["action"] == "answer" else []
    refs.extend(ref for part in public.get("parts", []) if part["action"] == "answer"
                for ref in part["sources"])
    for ref in refs:
        if ref["kind"] == "faq" and ref.get("claim") != "Альтернативное разъяснение":
            conn.execute("""INSERT INTO assembly_faq_uses(turn_id,faq_id)
                    SELECT %s,id FROM faq_entries WHERE current_fragment_id=%s ON CONFLICT DO NOTHING""",
                             (turn["id"], ref["fragment_id"]))


def period():
    today = datetime.now(MOSCOW).date()
    start = datetime.combine(today-timedelta(days=29), time.min, MOSCOW)
    until = datetime.combine(today+timedelta(days=1), time.min, MOSCOW)
    return start, until


PERIOD_GAPS = """
    SELECT t.id AS turn_id,t.case_id,t.question,t.created_at FROM assembly_gaps g
    JOIN turns t ON t.id=g.turn_id
    WHERE t.status='completed' AND t.updated_at >= %(start)s AND t.updated_at < %(until)s
"""

POPULAR = """
WITH issued AS (
    SELECT DISTINCT t.id AS turn_id,s.id AS document_id
    FROM turns t JOIN conversations c ON c.id=t.conversation_id
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(t.result->'sources','[]') ||
       coalesce((SELECT jsonb_agg(ref) FROM jsonb_array_elements(t.result->'parts') p,
                 jsonb_array_elements(p->'sources') ref WHERE p->>'action' IN ('answer','escalate')), '[]')) src
    JOIN source_fragments f ON f.id=(src->>'fragment_id')::uuid
    JOIN source_revisions r ON r.id=f.revision_id
    JOIN sources s ON s.id=r.source_id
    WHERE """ + VISIBLE_DOCUMENT + """
      AND c.owner_id LIKE 'guest:%%' AND src->>'kind'='document'
      AND t.status='completed' AND t.result->>'action' IN ('answer','escalate')
      AND t.updated_at >= %(start)s AND t.updated_at < %(until)s
)
SELECT s.id AS document_id,r.id AS revision_id,r.title,r.version,count(*) AS issues
FROM issued JOIN sources s ON s.id=issued.document_id JOIN source_revisions r ON r.id=s.current_revision_id
GROUP BY s.id,r.id ORDER BY issues DESC,r.title,s.id
"""

PROBLEMS = """
WITH gaps AS (""" + PERIOD_GAPS + """), evidence AS (
    SELECT f.id AS fragment_id,s.id AS document_id,r.id AS revision_id,r.title,r.version,f.anchor
    FROM source_fragments f JOIN source_revisions r ON r.id=f.revision_id JOIN sources s ON s.id=r.source_id
    WHERE """ + VISIBLE_DOCUMENT + """
), metrics AS (
    SELECT g.turn_id::text AS event_id,e.fragment_id,TRUE AS gap FROM gaps g
    LEFT JOIN (assembly_gap_evidence a JOIN evidence e USING(fragment_id)) ON a.turn_id=g.turn_id
    UNION ALL
    SELECT f.id::text,e.fragment_id,FALSE FROM assembly_quality_checks check_result
    JOIN faq_entries f ON f.id=check_result.faq_id
    JOIN evidence e ON e.fragment_id=check_result.fragment_id
    WHERE check_result.outcome='conflict' AND f.status<>'withdrawn'
      AND check_result.faq_revision_id=f.current_revision_id
)
SELECT e.document_id,e.revision_id,e.fragment_id,e.title,e.version,e.anchor,
    count(DISTINCT event_id) FILTER (WHERE gap) AS gaps,
    count(DISTINCT event_id) FILTER (WHERE NOT gap) AS conflicts
FROM metrics LEFT JOIN evidence e USING(fragment_id)
GROUP BY e.document_id,e.revision_id,e.fragment_id,e.title,e.version,e.anchor
ORDER BY gaps DESC,conflicts DESC,e.title,e.anchor
"""


def summary(conn, actor):
    start, until = period()
    params = dict(start=start, until=until, actor=actor)
    groups = conn.execute("""
        WITH gaps AS (""" + PERIOD_GAPS + """)
        SELECT g.id,g.topic,g.conditions,count(DISTINCT a.case_id) AS count
        FROM assembly_gap_members a JOIN assembly_gap_groups g ON g.id=a.group_id
        JOIN gaps ON gaps.case_id=a.case_id GROUP BY g.id HAVING count(DISTINCT a.case_id)>=2
        ORDER BY count DESC,g.topic,g.id
    """, params).fetchall()
    from .gap_groups import public_conditions
    for group in groups:
        group["conditions"] = public_conditions(group["conditions"])
    return {
        "period": {"start": start.date(), "end": (until-timedelta(days=1)).date(), "timezone": "Europe/Moscow"},
        "documents": conn.execute(POPULAR, params).fetchall(),
        "problems": conn.execute(PROBLEMS, params).fetchall(),
        "gaps_total": conn.execute("SELECT count(DISTINCT case_id) AS n FROM (" + PERIOD_GAPS + ") g",
                                   params).fetchone()["n"],
        "groups": groups,
        "ratings": conn.execute("""
            SELECT count(*) FILTER (WHERE r.rating='up') AS up,count(*) FILTER (WHERE r.rating='down') AS down
            FROM assembly_message_ratings r JOIN messages m ON m.id=r.message_id
            WHERE m.created_at >= %(start)s AND m.created_at < %(until)s
        """, params).fetchone(),
    }


def group_questions(conn, group_id):
    start, until = period()
    # 'turns.question' retains the original request after numbered clarification.
    return conn.execute("""
        WITH gaps AS (""" + PERIOD_GAPS + """)
        SELECT DISTINCT ON (g.case_id) g.case_id,g.question FROM gaps g
        JOIN assembly_gap_members m ON m.case_id=g.case_id WHERE m.group_id=%(group)s
        ORDER BY g.case_id,g.created_at,g.turn_id
    """, dict(start=start, until=until, group=group_id)).fetchall()
