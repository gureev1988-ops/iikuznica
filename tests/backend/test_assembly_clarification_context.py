import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.assembly import engine as assembly
from backend.assembly.corpus import seed
from backend.assembly.guidance import next_hint
from backend.contracts.api import MessageRequest
from backend.contracts.engine import AnswerResult, Citation, ClarificationOption, EngineUnavailable
from backend.db import connect
from backend.dialog import Actor, DialogueService
from backend.runtime import claim, run_once


def test_unknown_fact_keeps_previous_conditions_for_all_rule_comparisons(monkeypatch):
    search = SimpleNamespace(snapshot=lambda fragments: ({}, lambda query: ([], []), {}, {}))
    engine = assembly.A1Engine("", search=search)
    engine.records = []
    monkeypatch.setattr(assembly, "linked_norms", lambda originals, records: {})
    seen = []

    def primary(question, lookup, **kwargs):
        seen.append(question)
        return {"action": "clarify", "sources": [], "message": "На каком этапе проект?"}

    def compare(question, resolved, *args):
        seen.append(question)
        return AnswerResult("escalate", "Уточним оставшееся условие.", escalation_question=resolved)

    monkeypatch.setattr(assembly.core, "answer", primary)
    monkeypatch.setattr(engine, "_conditional_rules", compare)
    request = SimpleNamespace(question="Кто назначает руководителя проекта?", history=(),
                              sources=SimpleNamespace(fragments=()), clarification_count=2,
                              pending_clarification=None,
                              clarification=dict(key="stage", value="Не знаю", original="Не знаю",
                                                 prompt="На каком этапе?", choices=["Пресейл", "Инициация"]),
                              conditions={"directorate": "Дирекция ИБ", "signed": "Договор не подписан",
                                          "stage": "Не знаю"})
    engine._answer(request, SimpleNamespace(chat=None, decisions=None))
    assert len(seen) == 2
    for question in seen:
        assert "Дирекция ИБ" in question and "Договор не подписан" in question
        assert "Сотрудник не знает: На каком этапе?" in question


@pytest.mark.parametrize("reply,next_action", [
    ("Расшифруйте ДБРЦТ", "answer"),
    ("Чем они отличаются?", "clarify"),
    ("ДБРЦТ?", "escalate"),
])
def test_question_about_shown_choices_can_be_answered_before_employee_selects(dsn, tmp_path, monkeypatch, reply, next_action):
    seed(dsn, tmp_path)
    employee = Actor("employee", frozenset({"employee"}), "employee@example.test", channel="guest")
    conversation = uuid4()
    topic = "Кто назначает руководителя проекта?"
    prompt = "Какая дирекция ведёт проект?"
    options = (ClarificationOption("db", "ДБРЦТ", {"directorate": "ДБРЦТ"}),
               ClarificationOption("ib", "Дирекция ИБ", {"directorate": "Дирекция ИБ"}))
    observed = []
    model_payloads = []
    engine = assembly.A1Engine(dsn, search=object())

    def fake_choice(case, body, stage):
        assert stage == "clarification-meaning"
        model_payloads.append(json.loads(body["messages"][1]["content"])["question"])
        return {"text": '{"route":"about_choices","option_id":null,"facts":{}}'}

    def answer(request):
        observed.append(request)
        if len(observed) == 1:
            return AnswerResult("clarify", prompt, clarification_key="directorate", options=options)
        source = request.sources.fragments[0]
        citation = Citation(source.document_id, source.revision_id, source.fragment_id, source.text)
        if len(observed) == 2:
            return AnswerResult(next_action, source.text + " [1]", citations=(citation,),
                                options=options if next_action == "clarify" else ())
        return AnswerResult("answer", source.text + " [1]", citations=(citation,))

    monkeypatch.setattr(assembly, "ModelCalls", lambda _: SimpleNamespace(chat=fake_choice))
    monkeypatch.setattr(engine, "answer", answer)
    service = DialogueService(dsn=dsn, engine=engine, resolve_actor=lambda *_: employee)

    def send(text, **fields):
        with connect(dsn) as conn:
            return service.admit(conn, employee, conversation, MessageRequest(
                text=text, client_message_id=str(uuid4()), **fields))

    def process():
        service.answer_job(claim(dsn, "interactive"))

    first = send(topic)
    process()
    with connect(dsn) as conn:
        clarification = service.get_turn(conn, employee, first.turn_id)["clarifications"][0]
    continuation = dict(case_id=first.case_id, clarification_id=clarification["id"],
                        clarification_version=clarification["version"])
    second = send(reply, **continuation)
    assert second.case_id == first.case_id
    process()
    with connect(dsn) as conn:
        result = service.get_turn(conn, employee, second.turn_id)["result"]
        if next_action == "answer":
            assert result["action"] == "answer" and len(result["sources"]) == 1
        else:
            assert result["action"] == "clarify" and prompt in result["text"] and "1. ДБРЦТ" in result["text"]
        assert [item["id"] for item in service.open_actions(conn, employee, conversation)["clarifications"]] == [clarification["id"]]
        assert not service.open_actions(conn, employee, conversation)["offers"]
        assert conn.execute("SELECT conditions FROM cases WHERE id=%s", (first.case_id,)).fetchone()["conditions"] == {}
        hint = next_hint(conn, {"id": uuid4(), "conversation_id": conversation})
    assert (hint.stage, hint.case_id, hint.example) == ("answer", first.case_id, "2")
    followup_context = observed[1]
    assert followup_context.question == reply
    assert followup_context.history[0].text == topic
    assert all(s in followup_context.history[1].text for s in (prompt, "Дирекция ИБ", "ДБРЦТ"))
    assert followup_context.pending_clarification == clarification["id"]
    assert followup_context.clarification is None
    assert model_payloads == [{"question": topic, "prompt": prompt, "conditions": {},
                               "options": [{"id": o.id, "text": o.text} for o in options], "reply": reply}]

    assert send("2", **continuation).case_id == first.case_id
    process()
    assert observed[2].question == topic
    assert observed[2].conditions == {"directorate": "Дирекция ИБ"}
    assert len(model_payloads) == 1


def test_a1_explains_choice_references_using_the_actual_prompt_context_and_can_retry(dsn, tmp_path, monkeypatch):
    seed(dsn, tmp_path)
    owner = uuid4()
    employee = Actor(f"guest:{owner}", frozenset({"employee"}), "employee@example.test", channel="guest")
    conversation = uuid4()
    session = {"id": owner, "conversation_id": conversation}
    original = "Кто назначает руководителя проекта?"
    reply = "Чем они отличаются?"
    resolved = "Чем отличаются ДБРЦТ и Дирекция ИБ?"
    key = ("DB", "1")
    lookups = []
    resolutions = []

    def snapshot(fragments):
        source = fragments[0]
        passage = dict(doc=key[0], ref=key[1], title=source.title, section="Раздел", text=source.text)

        def lookup(question):
            lookups.append(question)
            return [key], [dict(passage, logit=1.0)]

        return {key: passage}, lookup, {key: source}, {}

    def chat(case, body, stage):
        payload = json.loads(body["messages"][1]["content"])
        if stage == "clarification-meaning":
            return {"text": '{"route":"about_choices","option_id":null,"facts":{}}'}
        if stage == "dialogue-state":
            assert payload["question"] == reply
            past, = payload["sources"]
            assert past["user"] == original
            assert "1. ДБРЦТ" in past["assistant"] and "2. Дирекция ИБ" in past["assistant"]
            resolutions.append(payload)
            if len(resolutions) == 1:
                raise EngineUnavailable("First resolution attempt failed")
            return {"ok": True, "text": json.dumps({"relation": "follow", "turn": 1, "query": resolved})}
        if stage == "evidence-core":
            assert payload["question"] == resolved
            claim, = payload["sources"][0]["claims"]
            return {"ok": True, "text": json.dumps({"action": "answer", "ids": [claim["id"]]})}
        assert stage == "document-reference"
        return {"ok": True, "text": "{}"}

    def decide(body):
        assert json.loads(body["state"])["employee_question"] == resolved
        return {"response": {"answers": {
            "route": {"type": "choice", "choice": "answer", "confidence": 1.0},
            "missing": {"type": "choice", "choice": "other"},
            "scope": {"type": "choice", "choice": "project", "confidence": 1.0},
            "grounded": {"type": "noul", "noul": 1.0},
            "complete": {"type": "noul", "noul": 1.0},
        }}}

    engine = assembly.A1Engine(dsn, search=SimpleNamespace(snapshot=snapshot))
    engine.records = []
    monkeypatch.setattr(assembly, "ModelCalls", lambda _: SimpleNamespace(chat=chat, decisions=decide))
    service = DialogueService(dsn=dsn, engine=engine, resolve_actor=lambda *_: employee)

    def send(text, **fields):
        with connect(dsn) as conn:
            return service.admit(conn, employee, conversation, MessageRequest(
                text=text, client_message_id=str(uuid4()), **fields))

    def process():
        assert run_once(dsn, "interactive", {"answer_turn": service.answer_job},
                        terminal_handlers={"answer_turn": service.terminal_outcome}) is not None

    first = send(original)
    process()
    lookups.clear()
    with connect(dsn) as conn:
        clarification, = service.get_turn(conn, employee, first.turn_id)["clarifications"]
    second = send(reply, case_id=first.case_id, clarification_id=clarification["id"])
    with connect(dsn) as conn:
        conn.execute("UPDATE jobs SET max_attempts=1 WHERE payload->>'turn_id'=%s", (str(second.turn_id),))
    process()
    with connect(dsn) as conn:
        assert service.get_turn(conn, employee, second.turn_id)["status"] == "technical_failure"
        assert service.retry(conn, employee, second.turn_id, 1) == second
    process()
    repeated = send(reply, case_id=first.case_id, clarification_id=clarification["id"])
    process()
    with connect(dsn) as conn:
        for answered in (second, repeated):
            result = service.get_turn(conn, employee, answered.turn_id)["result"]
            assert result["action"] == "answer" and result["sources"]
        hint = next_hint(conn, session)
        assert (hint.stage, hint.case_id, hint.example) == ("answer", first.case_id, "2")
    assert len(resolutions) == 3 and lookups and set(lookups) == {resolved}
