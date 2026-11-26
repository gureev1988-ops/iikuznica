import json
from collections import deque
from html import unescape
from uuid import UUID, uuid4

import httpx
import pytest

from backend.dion.client import MAX_UPDATE_ID, DionAPIError, DionClient, DionConfig

BOT = UUID("09889fe6-d888-451f-a98d-8556a1000001")
USER = UUID("09889fe6-d888-451f-a98d-8556a1000002")
CHAT = UUID("09889fe6-d888-451f-a98d-8556a1000003")
FIRST_UPDATE = 1770714680415030001


class DionSimulator:
    """DION Bot API v2 envelopes over HTTP, including asynchronous receipts."""

    def __init__(self):
        self.updates = []
        self.offsets = []
        self.auth_count = 0
        self.expired = False
        self.sends = []
        self.outcomes = deque()
        self.counter = FIRST_UPDATE

    def client(self):
        return DionClient(
            DionConfig("https://bots.example.invalid", "bot@example.invalid", "test-password"),
            transport=httpx.MockTransport(self.request),
        )

    def add(self, envelope, *, update_id=None):
        if update_id is None:
            update_id = self.counter
            self.counter += 1
        self.updates.append({"update_id": update_id, **envelope})

    def text(self, text, *, chat=CHAT, user=USER, message_id=None, reply=None, kind="dm"):
        identifier = message_id or uuid4()
        message = {
            "message_id": str(identifier), "from": {"id": str(user), "is_bot": user == BOT},
            "chat": {"id": str(chat), "type": kind}, "message_type": "regular", "text": text,
        }
        if reply:
            message["reply_to_message"] = {"message_id": str(reply)}
        self.add({"message": message})
        return identifier

    def request(self, request):
        path = request.url.path
        if path == "/platform/v1/token":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "email": "bot@example.invalid", "password": "test-password",
            }
            self.auth_count += 1
            self.expired = False
            return httpx.Response(200, json={
                "access_token": f"test-token-{self.auth_count}", "user": {"id": str(BOT)},
            })
        assert request.headers["Authorization"] == f"Bearer test-token-{self.auth_count}"
        if self.expired:
            return httpx.Response(401, json={"ok": False, "error_code": 401})

        if path == "/chats/v2/getUpdates":
            assert request.method == "GET"
            params = request.url.params
            assert 0 <= int(params["timeout"]) <= 50
            offset = params.get("offset")
            self.offsets.append(offset)
            updates = [item for item in self.updates if offset is None or int(item["update_id"]) >= int(offset)]
            return httpx.Response(200, json={"ok": True, "result": updates[:int(params["limit"])]})

        assert path == "/chats/v2/sendMessage"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["parse_mode"] == "HTML"
        assert 1 <= len(body["text"]) <= 4096
        self.sends.append(body)
        intermediate_id = uuid4()
        body["intermediate_id"] = intermediate_id
        outcome = self.outcomes.popleft() if self.outcomes else "accepted"
        if outcome == "timeout":
            raise httpx.ReadTimeout("provider response lost")
        if outcome == "rejected":
            return httpx.Response(403, json={"ok": False, "error_code": 403})
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": str(intermediate_id), "chat": {"id": body["chat_id"], "type": "dm"},
            "text": unescape(body["text"]), "message_type": "regular",
            "from": {"id": str(BOT), "is_bot": True},
        }})

    def settle(self, sent, *, success=True, same_id=False):
        intermediate_id = sent["intermediate_id"]
        if not success:
            self.add({"message_failed": {"message_id": str(intermediate_id), "chat_id": sent["chat_id"]}})
            return None
        message_id = intermediate_id if same_id else uuid4()
        self.add({"message": {
            "message_id": str(message_id), "intermediate_id": str(intermediate_id),
            "from": {"id": str(BOT), "is_bot": True},
            "chat": {"type": "dm", "id": sent["chat_id"]},
            "message_type": "regular", "text": unescape(sent["text"]),
        }})
        return message_id


def test_dion_bearer_refresh_exact_offset_and_send_envelope():
    provider = DionSimulator()
    client = provider.client()
    assert client.bot_id == BOT
    provider.expired = True
    case_message = provider.text("Вопрос")
    assert client.get_updates(offset=FIRST_UPDATE, limit=5, timeout=37)[0]["update_id"] == FIRST_UPDATE
    assert provider.auth_count == 2
    assert provider.offsets == [str(FIRST_UPDATE)]
    output = client.send_message(chat_id=CHAT, text="a < b & b > c", reply_to=case_message)
    sent = provider.sends[-1]
    assert output == str(sent["intermediate_id"])
    assert sent["text"] == "a &lt; b &amp; b &gt; c"
    assert sent["reply_parameters"] == {"message_id": str(case_message)}
    client.close()


@pytest.mark.parametrize("invalid_id", [float(FIRST_UPDATE), True, MAX_UPDATE_ID + 1, "-1"])
def test_dion_rejects_lossy_offsets(invalid_id):
    with pytest.raises(DionAPIError):
        DionSimulator().client().get_updates(offset=invalid_id)


@pytest.fixture
def dion(dsn):
    from backend.contracts.engine import build_engine
    from backend.contracts.fixtures import seed_fixture_sources
    from backend.db import connect
    from backend.dialog import Actor, DialogueService
    from backend.dion import DionAdapter, queue_reply
    from backend.mailfaq import MailConfig

    employee = Actor("alice", frozenset({"employee"}), "alice@example.invalid", channel="dion")

    def resolve(external_user_id):
        return employee if str(USER) == external_user_id else None

    provider = DionSimulator()
    client = provider.client()
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
    dialogue = DialogueService(
        dsn=dsn, engine=build_engine("fixture-alpha"), queue_reply=queue_reply,
        mail_config=MailConfig(
            sender="assistant@example.invalid",
            recipient="help@example.invalid",
            trusted_senders=("help@example.invalid",),
        ),
    )
    adapter = DionAdapter(dsn, client, dialogue, resolve, public_base_url="https://assistant.example.invalid")
    yield provider, adapter, dialogue
    client.close()


def _run(dsn, queue, handlers):
    from backend.runtime import run_once

    assert run_once(dsn, queue, handlers) is not None


def _state(dsn):
    from backend.db import connect

    with connect(dsn) as conn:
        return {
            "attempts": conn.execute("SELECT * FROM delivery_attempts ORDER BY started_at").fetchall(),
            "turns": conn.execute("SELECT * FROM turns ORDER BY created_at, question").fetchall(),
            "offers": conn.execute("SELECT * FROM offers ORDER BY created_at, id").fetchall(),
            "outbox": conn.execute("SELECT * FROM dion_outbox ORDER BY effect_key").fetchall(),
            "mail": conn.execute("SELECT * FROM mail_requests").fetchall(),
        }


def test_poll_cursor_and_feed_jobs_commit_and_deduplicate(dsn, dion, monkeypatch):
    from backend.db import connect
    from backend.dion import adapter as module

    provider, adapter, _ = dion
    first = provider.text("первый")
    provider.text("первый", message_id=first)
    provider.text("второй")
    provider.text("личные данные группы", kind="group")
    provider.updates.reverse()

    original_classify = module._classify

    def crash_in_batch(item, bot_id):
        if item["update_id"] == FIRST_UPDATE + 2:
            raise RuntimeError("worker stopped before commit")
        return original_classify(item, bot_id)

    monkeypatch.setattr(module, "_classify", crash_in_batch)
    with pytest.raises(RuntimeError, match="before commit"):
        adapter.poll_once(timeout=0)
    with connect(dsn) as conn:
        assert conn.execute("SELECT * FROM dion_cursors").fetchone() is None
        assert conn.execute("SELECT * FROM dion_updates").fetchone() is None
    monkeypatch.setattr(module, "_classify", original_classify)

    assert adapter.poll_once(timeout=0) == 3
    assert adapter.poll_once(timeout=0) == 0
    assert provider.offsets == [None, None, str(FIRST_UPDATE + 4)]
    assert adapter.route_once() == 2
    assert adapter.route_once() == 0
    with connect(dsn) as conn:
        ingress = conn.execute("SELECT * FROM dion_updates ORDER BY update_id").fetchall()
        assert [row["update_id"] for row in ingress] == [FIRST_UPDATE, FIRST_UPDATE + 2, FIRST_UPDATE + 3]
        assert ingress[-1]["payload"] == {}
        assert ingress[0]["case_id"] != ingress[1]["case_id"]
        jobs = conn.execute("SELECT payload FROM jobs ORDER BY created_at").fetchall()
        assert [row["payload"]["turn_id"] for row in jobs] == [
            str(ingress[0]["turn_id"]), str(ingress[1]["turn_id"]),
        ]


def test_dion_ambiguous_confirmation_and_late_delivery_use_original_case(dsn, dion):
    from backend.mailfaq import MailService
    from backend.runtime import run_once

    provider, adapter, dialogue = dion
    original_message = provider.text("Нужно официальное разъяснение")
    provider.text("Нужно официальное разъяснение")
    assert adapter.poll_once(timeout=0) == 2
    assert adapter.route_once() == 2
    for _ in range(2):
        _run(dsn, "interactive", {"answer_turn": dialogue.answer_job})
    for _ in range(2):
        _run(dsn, "background", adapter.handlers)
    first_offer_message = provider.settle(provider.sends[0], same_id=True)
    provider.settle(provider.sends[1])
    assert adapter.poll_once(timeout=0) == 2

    provider.text("да")
    adapter.poll_once(timeout=0)
    assert adapter.route_once() == 1
    assert _state(dsn)["mail"] == []
    _run(dsn, "background", adapter.handlers)
    assert "Уточните" in provider.sends[-1]["text"]
    for offer in _state(dsn)["offers"]:
        assert str(offer["id"]) in provider.sends[-1]["text"]

    provider.text("да", reply=first_offer_message)
    provider.text("да", reply=first_offer_message)
    adapter.poll_once(timeout=0)
    assert adapter.route_once() == 2
    state = _state(dsn)
    assert len(state["mail"]) == 1
    request = state["mail"][0]
    assert request["channel"] == "dion"
    assert {offer["status"] for offer in state["offers"]} == {"confirmed", "open"}

    mail = MailService(dsn, dialogue.mail_config)
    handlers = {**adapter.handlers, **mail.handlers}
    while run_once(dsn, "background", handlers) is not None:
        pass
    assert len(mail.simulator.sent) == 1

    mail.simulator.reply(
        mail.simulator.sent[0], "Официальный ответ — два дня.",
        message_id="<official-dion@example.invalid>",
    )
    assert mail.poll_inbox() == 1
    _run(dsn, "background", handlers)
    sent = provider.sends[-1]
    assert sent["reply_parameters"] == {"message_id": str(original_message)}
    assert str(request["case_id"]) in sent["text"]
    assert f"https://assistant.example.invalid/v1/turns/{request['turn_id']}" in sent["text"]
    assert "два дня" in sent["text"]


def test_dion_async_failure_and_timeout_never_count_as_sent(dsn, dion):
    from backend.db import connect

    provider, adapter, dialogue = dion
    provider.text("Сколько дней рассматривают заявку?")
    provider.text("Сколько дней рассматривают заявку?")
    provider.outcomes.extend(("accepted", "timeout"))
    adapter.poll_once(timeout=0)
    adapter.route_once()
    for _ in range(2):
        _run(dsn, "interactive", {"answer_turn": dialogue.answer_job})
        _run(dsn, "background", adapter.handlers)

    attempts = _state(dsn)["attempts"]
    assert [row["state"] for row in attempts] == ["delivery_unknown", "delivery_unknown"]
    assert attempts[0]["provider_id"] == str(provider.sends[0]["intermediate_id"])
    assert attempts[1]["provider_id"] is None
    provider.settle(provider.sends[0], success=False)
    provider.settle(provider.sends[1])
    adapter.poll_once(timeout=0)
    attempts = _state(dsn)["attempts"]
    assert [row["state"] for row in attempts] == ["failed", "delivery_unknown"]
    assert attempts[0]["detail"] == "dion_message_failed"
    assert adapter.reconcile_pending() == 0
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM events WHERE kind = 'dion.delivery_failed'").fetchone()["n"] == 1
        conn.execute(
            """
            UPDATE jobs SET state = 'queued', available_at = now()
            WHERE business_key = %s
            """,
            (attempts[1]["effect_key"],),
        )
    _run(dsn, "background", adapter.handlers)
    assert len(provider.sends) == 2


def test_dion_routes_numbered_clarification_to_the_same_case(dsn, dion):
    from backend.db import connect

    provider, adapter, dialogue = dion
    provider.text("Как подать заявку?")
    adapter.poll_once(timeout=0)
    adapter.route_once()
    _run(dsn, "interactive", {"answer_turn": dialogue.answer_job})
    _run(dsn, "background", adapter.handlers)
    prompt = provider.sends[-1]
    assert "1. Сотруднику" in prompt["text"]
    reply_to = provider.settle(prompt)
    adapter.poll_once(timeout=0)

    provider.text("1", reply=reply_to)
    adapter.poll_once(timeout=0)
    assert adapter.route_once() == 1
    with connect(dsn) as conn:
        case = conn.execute("SELECT id, conditions FROM cases").fetchone()
        assert case["conditions"] == {"requester": "employee"}
        assert conn.execute("SELECT status FROM clarifications").fetchone()["status"] == "resolved"
        turns = conn.execute("SELECT case_id FROM turns").fetchall()
        assert [row["case_id"] for row in turns] == [case["id"], case["id"]]


def test_dion_rechecks_source_applicability_before_deferred_send(dsn, dion):
    from backend.contracts.fixtures import seed_fixture_sources
    from backend.db import connect

    provider, adapter, dialogue = dion
    provider.text("Сколько дней рассматривают заявку?")
    adapter.poll_once(timeout=0)
    adapter.route_once()
    _run(dsn, "interactive", {"answer_turn": dialogue.answer_job})
    assert _state(dsn)["outbox"][0]["source_refs"]

    with connect(dsn) as conn:
        seed_fixture_sources(conn, active_version=2)
    _run(dsn, "background", adapter.handlers)
    assert provider.sends == []
    attempt = _state(dsn)["attempts"][0]
    assert attempt["state"] == "failed"
    assert attempt["detail"] == "dion_source_no_longer_available"
