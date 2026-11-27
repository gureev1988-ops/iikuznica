"""Exercise the pilot's operator check and background FAQ review over loopback HTTP."""
import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from backend.assembly import quality
from backend.assembly.corpus import seed
from backend.assembly.quality_seed import seed_quality
from backend.db import connect


@pytest.fixture
def pilot(monkeypatch):
    requests = []
    blocked = []
    resolve = socket.getaddrinfo

    def only_loopback(host, *args, **kwargs):
        if host not in ("localhost", "127.0.0.1", "::1"):
            blocked.append(host)
            raise AssertionError(f"Unexpected model network destination: {host}")
        return resolve(host, *args, **kwargs)

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            role = self.path.removeprefix("/internal/")
            requests.append((role, self.headers.get("Authorization"), body))
            if role == "decider":
                if self.server.decider_status == "failed":
                    self.send_error(401)
                    return
                answers = {}
                for name, question in body["questions"].items():
                    if question["type"] == "noul":
                        answers[name] = {"type": "noul", "noul": 1.0}
                    else:
                        options = list(question["criteria"])
                        answers[name] = {"type": "choice", "choice": options[0], "confidence": 1.0,
                                         "probabilities": {key: float(key == options[0]) for key in options}}
                reply = {"answers": answers}
                if self.server.decider_status != "unverified":
                    reply["model"] = body["model"]
                data = json.dumps(reply).encode()
                media = "application/json"
            else:
                frame = {"model": body["model"], "choices": [
                    {"delta": {"content": self.server.chat_response(body)}, "finish_reason": "stop"}
                ]}
                data = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
                media = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("II_MODEL_PROFILE", "local")
    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        for role in ("gemma", "qwen", "decider"):
            prefix = "II_LOCAL_" + role.upper()
            monkeypatch.setenv(prefix + "_URL", f"http://127.0.0.1:{server.server_port}/internal/{role}")
            monkeypatch.setenv(prefix + "_MODEL", role + "-on-prem")
            monkeypatch.setenv(prefix + "_TOKEN", role + "-test-token")
        monkeypatch.setattr(socket, "getaddrinfo", only_loopback)
        server.chat_response = lambda body: "OK"
        server.decider_status = "ok"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, requests
        finally:
            server.shutdown()
            thread.join()
    assert not blocked


@pytest.mark.parametrize("decider_status", ["ok", "failed", "unverified"])
def test_operator_check_reaches_three_configured_models(pilot, monkeypatch, capsys, decider_status):
    from backend.assembly.check_models import main

    server, requests = pilot
    server.decider_status = decider_status
    monkeypatch.setattr(sys, "argv", ["check_models"])
    assert main() == (0 if decider_status == "ok" else 1)
    output = capsys.readouterr().out
    assert "-test-token" not in output
    rows = {row["role"]: row for row in map(json.loads, output.splitlines())}
    assert {role: row["status"] for role, row in rows.items()} == {
        "gemma": "ok", "qwen": "ok", "decider": decider_status
    }
    for role, row in rows.items():
        if row["status"] == "ok":
            assert row["served_model"] == role + "-on-prem"
    assert sorted(role for role, _, _ in requests) == ["decider", "gemma", "qwen"]
    for role, token, body in requests:
        assert token == f"Bearer {role}-test-token"
        assert body["model"] == role + "-on-prem"
        if role == "decider":
            assert body["questions"] and "state" in body and "messages" not in body
        else:
            assert body["messages"] and body["stream"] is True


def test_background_worker_saves_grounded_faq_conflict_without_openrouter(pilot, dsn, tmp_path):
    server, requests = pilot
    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    with connect(dsn) as conn:
        faq = conn.execute("SELECT id, request_id FROM faq_entries").fetchone()
        # A condition takes this mail through semantic review instead of the exact-quantity shortcut.
        conn.execute("UPDATE mail_requests SET conditions=%s WHERE id=%s",
                     (Jsonb({"вид оценки": "точная"}), faq["request_id"]))
        assert quality.review_after_mail(conn, faq["id"])["pending"]

    def read_evidence(body):
        case = json.loads(body["messages"][-1]["content"])
        source = next(s for s in case["sources"] if s["anchor"] == "1.6" and "7 рабочих дней" in s["text"])
        return json.dumps({"outcome": "conflict", "index": source["index"], "quote": "7 рабочих дней",
                           "claim": "6 рабочих дней", "reason": "Срок из письма расходится с пунктом 1.6."})

    server.chat_response = read_evidence
    assert quality.recheck_revision(SimpleNamespace(dsn=dsn))
    assert [(role, token) for role, token, _ in requests] == [("gemma", "Bearer gemma-test-token")]
    with connect(dsn) as conn:
        result = conn.execute("""
            SELECT ck.outcome, ck.quote, fr.anchor, ck.retry_after
            FROM assembly_quality_checks ck JOIN source_fragments fr ON fr.id=ck.fragment_id
            WHERE ck.faq_id=%s
        """, (faq["id"],)).fetchone()
    assert result == {"outcome": "conflict", "quote": "7 рабочих дней", "anchor": "1.6", "retry_after": None}


def test_local_gemma_groups_gap_and_returns_mail_to_originating_chat(pilot, dsn, monkeypatch):
    from uuid import uuid4

    from backend.assembly.engine import A1Engine, core
    from backend.assembly.gap_groups import classify_gap
    from backend.assembly.smtp_gateway import BOT, DOMAIN, QUALITY
    from backend.assembly.statistics import GROUP_JOB
    from backend.contracts.api import MessageRequest
    from backend.dialog import Actor, DialogueService
    from backend.mailfaq import MailConfig, MailService
    from backend.runtime import run_once

    server, requests = pilot
    question = "Кто назначит руководителя для экспертизы?"
    official = "На экспертизу руководителя назначает координатор."
    topic = "Назначение руководителя проекта"

    def process(body):
        case = json.loads(body["messages"][-1]["content"])
        payload = json.loads(case["question"]) if isinstance(case["question"], str) else case["question"]
        if payload.get("unresolved") == question:
            return json.dumps({"id": None, "topic": topic, "fallback": topic})
        if payload.get("employee_question") == question:
            assert case["sources"][0]["answer"] == official
            return json.dumps({"id": 1, "scope": "general", "comparison": "gap"})
        assert payload["question"] == question and official in payload["new_mail"]
        return json.dumps({"kind": "substantive", "answer": [official]})

    server.chat_response = process
    config = MailConfig(sender=BOT, recipient=QUALITY, reply_to=BOT, trusted_senders=(QUALITY,))
    mail = MailService(dsn, config)
    # Fix the regulation-only outcome for an empty corpus. The A1 FAQ route, persistence,
    # worker leases, mail interpretation and cross-session retrieval use the application code.
    monkeypatch.setattr(core, "answer", lambda *_a, **_k: {"action": "escalate", "sources": [],
                                                            "message": "Уточнить у эксперта?"})
    dialog = DialogueService(dsn=dsn, engine=A1Engine(dsn), mail_config=config)

    def employee():
        uid = uuid4()
        return Actor(f"guest:{uid}", frozenset({"employee"}), f"guest-{uid.hex}@{DOMAIN}", "guest")

    actor, conversation = employee(), uuid4()
    with connect(dsn) as conn:
        turn = dialog.admit(conn, actor, conversation, MessageRequest(text=question, client_message_id="ask"))
    assert run_once(dsn, "interactive", {"answer_turn": dialog.answer_job})
    assert run_once(dsn, "background", {GROUP_JOB: lambda job: classify_gap(dsn, None, job)}, active_kinds=[GROUP_JOB])
    with connect(dsn) as conn:
        group = conn.execute("""SELECT g.topic FROM assembly_gap_members m
            JOIN assembly_gap_groups g ON g.id=m.group_id WHERE m.case_id=%s""", (turn.case_id,)).fetchone()
        assert group["topic"] == topic
        dialog.admit(conn, actor, conversation, MessageRequest(text="Да, передай", client_message_id="agree",
                                                                case_id=turn.case_id))
    assert run_once(dsn, "background", mail.handlers, active_kinds=mail.handlers)
    mail.simulator.reply(mail.simulator.sent[0], official, message_id="<expert@assembly.test>")
    assert mail.poll_inbox() == 1
    assert run_once(dsn, "background", mail.handlers, active_kinds=mail.handlers)
    with connect(dsn) as conn:
        history = dialog.history(conn, actor, conversation)["messages"]
        assert official in history[-1]["text"] and history[-1]["source_refs"][0]["kind"] == "faq"
        assert conn.execute("SELECT state FROM mail_requests").fetchone()["state"] == "resolved"
        assert conn.execute("SELECT count(*) n FROM faq_entries").fetchone()["n"] == 1
        other, second = employee(), uuid4()
        again = dialog.admit(conn, other, second, MessageRequest(text=question, client_message_id="repeat"))
    assert run_once(dsn, "interactive", {"answer_turn": dialog.answer_job})
    with connect(dsn) as conn:
        result = dialog.get_turn(conn, other, again.turn_id)["result"]
        assert official in result["text"] and result["sources"][0]["kind"] == "faq"
    assert [(role, token) for role, token, _ in requests] == [("gemma", "Bearer gemma-test-token")] * 3
