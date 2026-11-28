"""Ground semantic mail decisions in the exact open prompt and the employee's own words."""
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest

from backend.assembly import offer_replies
from backend.assembly.engine import A1Engine
from backend.contracts.engine import AnswerRequest, EngineUnavailable, HistoryMessage, SourceSnapshot


OFFER = {
    "question": "Кто замещает координатора Дирекции ИБ на время отпуска?",
    "prompt": "Порядок замещения не указан. Передать вопрос о замещении в Отдел качества?",
    "conditions": {"directorate": "ИБ"},
}


def model_reply(intent, evidence, recipient_evidence="", **overrides):
    return {"intent": intent, "evidence": evidence, "subject": "offered_question",
            "recipient": "quality_department", "commitment": "unconditional",
            "recipient_evidence": recipient_evidence, **overrides}


def interpret(monkeypatch, text, result):
    context = AnswerRequest(
        text, uuid4(), uuid4(), {}, (HistoryMessage("user", "В другой теме указаны банковские реквизиты."),),
        datetime.now(UTC) + timedelta(seconds=10), SourceSnapshot(()),
    )

    def chat(calls, role, body, stage):
        assert calls.request is context and (role, stage) == ("offer", "offer-meaning")
        system, user = body["messages"]
        assert system["role"] == "system" and text not in system["content"]
        assert user["role"] == "user"
        assert json.loads(user["content"]) == {"question": {
            "offered_question": OFFER["question"], "bot_prompt": OFFER["prompt"],
            "conditions": OFFER["conditions"], "recipient": "Отдел качества", "employee_reply": text,
        }, "sources": []}
        if isinstance(result, EngineUnavailable):
            raise result
        return {"text": json.dumps(result, ensure_ascii=False)} if not isinstance(result, str) else {"text": result}

    monkeypatch.setattr(offer_replies.ModelCalls, "chat", chat)
    return A1Engine("unused", search=object()).interpret_offer(text, offer=OFFER, context=context)


@pytest.mark.parametrize(("text", "intent", "recipient", "commitment"), [
    ("да, уточни у них", "confirm", "у них", "unconditional"),
    ("пусть отдел качества разберётся с этим", "confirm", "отдел качества", "unconditional"),
    ("Я не возражаю, свяжитесь с ними.", "confirm", "с ними", "unconditional"),
    ("Даю добро на это обращение.", "confirm", "", "unconditional"),
    ("Хочу получить от них официальный ответ.", "confirm", "от них", "unconditional"),
    ("лучше сначала покажи, что ты им напишешь", "ask_details", "им", "none"),
    ("не сейчас, сперва сам узнаю", "decline", "", "unconditional"),
    ("я вообще про другую дирекцию", "related_question", "", "none"),
    ("Если он завтра не ответит, тогда уточни у них", "clarify", "у них", "conditional"),
])
def test_interprets_free_replies_against_the_pending_question(monkeypatch, text, intent, recipient, commitment):
    raw = model_reply(intent, text, recipient, commitment=commitment)
    assert interpret(monkeypatch, text, raw) == {"intent": intent, "evidence": text}


@pytest.mark.parametrize(("text", "decision"), [
    ("Я не против, но не сейчас", model_reply("confirm", "Я не против, но не сейчас")),
    ("сначала покажи мне письмо", model_reply("confirm", "сначала покажи мне письмо")),
    ("Передавай при условии, что мне покажут письмо", model_reply("confirm", "Передавай")),
    ("если он попросит, уточни у них", model_reply("confirm", "если он попросит, уточни у них", "у них")),
    ("подожди до согласования", model_reply("confirm", "подожди до согласования", commitment="conditional")),
    ("Сможешь переслать туда?", model_reply("confirm", "Сможешь переслать туда?", "туда")),
    ("Передай...", model_reply("confirm", "Передай...")),
    ("Я цитирую «отправь».", model_reply("confirm", "Я цитирую «отправь».")),
    ("Передайте это главному инженеру", model_reply("confirm", "Передайте это главному инженеру", "главному инженеру")),
    ("Спроси у директора", model_reply("confirm", "Спроси у директора", "директора", recipient="different")),
    ("Уточни у них", model_reply("confirm", "Уточни у них", "в Отдел качества")),
    ("Проясни у них уже другой вопрос", model_reply("confirm", "Проясни у них уже другой вопрос",
                                                  "у них", subject="different")),
    ("Пожалуй", {"intent": "confirm", "evidence": "Пожалуй"}),
    ("Пожалуй", model_reply("approve", "Пожалуй")),
    ("Пожалуй", '{"intent":'),
    ("Пожалуй", EngineUnavailable("offline")),
])
def test_unsupported_decision_cannot_send_the_offer(monkeypatch, text, decision):
    assert interpret(monkeypatch, text, decision) == {"intent": "clarify", "evidence": ""}
