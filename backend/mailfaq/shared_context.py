"""Prepare a case snapshot within the common training mailbox's source permissions."""
from uuid import UUID


def shared_context(conn, case_id, turn_id, history, question, conditions):
    turns = {r["id"]: r["result"] or {} for r in conn.execute(
        "SELECT id,result FROM turns WHERE case_id=%s", (case_id,))}
    permissions = conn.execute("SELECT condition_sources FROM cases WHERE id=%s", (case_id,)).fetchone()["condition_sources"]
    history = [m for m in history if m["case_id"] == case_id]

    def result_refs(result):
        return [*result.get("sources", []), *result.get("condition_sources", []),
                *(ref for part in result.get("parts", []) for ref in part.get("sources", []))]

    evidence = [r for t in turns.values() for r in result_refs(t)]
    evidence.extend(r for refs in permissions.values() for r in refs)
    evidence.extend(r for msg in history for r in (msg.get("source_refs") or ()))
    ids = {UUID(str(ref["fragment_id"])) for ref in evidence}
    public = {r["id"] for r in conn.execute("""
        SELECT f.id FROM source_fragments f JOIN source_revisions v ON v.id=f.revision_id
        JOIN sources s ON s.id=v.source_id WHERE f.id=ANY(%s::uuid[])
          AND s.acl_actor_ids IS NULL AND v.acl_actor_ids IS NULL AND v.indexed_at IS NOT NULL
        """, (list(ids),))} if ids else set()

    def shareable(refs):
        return all(UUID(str(ref["fragment_id"])) in public for ref in refs)

    safe = []
    for msg in history:
        refs = msg.get("source_refs")
        if refs is None and msg["role"] == "assistant":
            refs = result_refs(turns.get(msg["turn_id"], {}))
        if shareable(refs or ()):
            safe.append(msg)
        else:
            safe.append(dict(msg, text="[Источник доступен только участнику личного диалога.]"))

    result = turns[turn_id]
    if not shareable(result_refs(result)):
        result = {}
        question = next((m["text"] for m in safe if m["role"] == "user"), "Уточните вопрос сотрудника.")
    conditions = {k: value for k, value in conditions.items() if shareable(permissions.get(k, []))}
    return question, conditions, safe, result
