from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from backend.assembly import pricing
from backend.assembly.guidance import latest_log, next_hint
from backend.db import connect
from backend.runtime import enqueue


def test_log_separates_the_latest_personal_turn_and_preserves_unknown_charges(dsn):
    with connect(dsn) as conn:
        owner = uuid4()
        chat = conn.execute("INSERT INTO conversations(owner_id) VALUES (%s) RETURNING id",
                            (f"guest:{owner}",)).fetchone()["id"]
        session = dict(id=owner, conversation_id=chat)
        assert latest_log(conn, session).status == "empty"
        assert next_hint(conn, session) == next_hint(conn, session)
        case = conn.execute("INSERT INTO cases(conversation_id) VALUES (%s) RETURNING id", (chat,)).fetchone()["id"]
        turn = conn.execute("""
            INSERT INTO turns(conversation_id,case_id,request_id,question,status)
            VALUES (%s,%s,'q1','Вопрос','completed') RETURNING id
        """, (chat, case)).fetchone()["id"]
        started = datetime.now(UTC)
        conn.execute("""
            INSERT INTO assembly_local_steps(turn_id,role,started_at,state,elapsed_ms)
            VALUES (%s,'A1',%s,'succeeded',2.5)
        """, (turn, started))
        background = enqueue(conn, queue="background", kind="mail.interpret", key="mail-test", payload={})
        for job in (None, background):
            # An expired background attempt on the same originating turn must
            # never appear in the employee's request log or its subtotal.
            conn.execute("""
                INSERT INTO model_calls(id,job_id,lease_token,turn_id,case_id,role,trace_id,model,route,state,expires_at)
                VALUES (%s,%s,%s,%s,%s,'answer','trace','model','route','outcome_unknown',now())
            """, (uuid4(), job, uuid4() if job else None, turn, case))
        view = latest_log(conn, session)
        assert view.incomplete and view.total_cost_usd is None
        assert [(s.local, s.cost_usd) for s in view.steps] == [(True, 0), (False, None)]
        assert view.steps[0].started_at == started and view.steps[0].elapsed_ms == 2.5
        assert view.steps[1].elapsed_ms is None
        conn.execute("""
            INSERT INTO turns(conversation_id,case_id,request_id,question,created_at)
            VALUES (%s,%s,'q2','Новый вопрос',clock_timestamp())
        """, (chat, case))
        latest = latest_log(conn, session)
        assert latest.turn_id != turn and latest.steps == []
        assert latest_log(conn, dict(id=uuid4(), conversation_id=uuid4())).status == "empty"


def test_rate_fallback_requires_fresh_matching_endpoint_and_cache_terms(monkeypatch):
    rates = {"prompt": Decimal("0.1"), "completion": Decimal("0.2"), "input_cache_read": Decimal("0.01")}
    monkeypatch.setattr(pricing, "_rates", {("model", "vendor/fp8"): (monotonic() + 30, "vendor", rates)})
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}
    assert pricing.charge("model", "vendor/fp8", "Vendor", usage) == Decimal("1.04")
    assert pricing.charge("model", "vendor/fp8", "Other", usage) is None
    assert pricing.charge("model", "vendor/fp8", "Vendor", {"prompt_tokens": 2}) is None
    del rates["input_cache_read"]
    assert pricing.charge("model", "vendor/fp8", "Vendor", usage) is None
