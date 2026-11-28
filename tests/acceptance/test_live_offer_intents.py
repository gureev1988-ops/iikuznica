"""Opt-in live evaluation of synthetic replies to one pending offer. No mail or database writes."""
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest

from backend.assembly.engine import A1Engine
from backend.assembly.models import json_reply
from backend.contracts.engine import AnswerRequest, SourceSnapshot


TRIALS = [
    ("Уточните у них этот момент от моего имени.", "confirm"),
    ("Буду благодарен, если это попадёт в отдел качества.", "clarify"),
    ("Обращайтесь туда, я согласна.", "confirm"),
    ("Пусть коллеги из отдела качества выяснят, что с этим делать.", "confirm"),
    ("Да, хочу официальный ответ на этот вопрос от них.", "confirm"),
    ("Только не сейчас. Сначала проверю сам.", "decline"),
    ("Стоп, я уже получил разъяснение.", "decline"),
    ("Можно посмотреть, что именно вы собираетесь послать?", "ask_details"),
    ("Прежде чем отвечу, объясните, кто получит сообщение.", "ask_details"),
    ("Если куратор к вечеру не вернётся, перешлите.", "clarify"),
    ("Подтверждаю только после согласования с начальником.", "clarify"),
    ("Отправьте это вместо отдела качества нашему директору.", "clarify"),
    ("Слова «передайте это туда» — это цитата из переписки.", "clarify"),
    ("У меня другой вопрос: как оформить командировку?", "new_question"),
    ("Я вообще про техническую дирекцию спрашивал.", "related_question"),
]


@pytest.mark.skipif(os.getenv("II_LIVE_OFFER_EVAL") != "1", reason="Set II_LIVE_OFFER_EVAL=1 for live offer evaluation")
@pytest.mark.parametrize(("reply", "expected"), TRIALS, ids=[f"reply-{i:02d}" for i in range(1, len(TRIALS) + 1)])
def test_live_offer_intent(reply, expected):
    completed = []

    class Recorder:
        def call(self, invoke, **metadata):
            result = invoke()
            completed.append(result.value)
            return result.value

    offer = {
        "question": "Кто замещает координатора отдела во время отпуска?",
        "prompt": "Правило замещения в инструкции не указано. Отправить этот вопрос в Отдел качества?",
        "conditions": {"directorate": "ИБ"},
    }
    context = AnswerRequest(reply, uuid4(), uuid4(), {}, (), datetime.now(UTC) + timedelta(seconds=16),
                            SourceSnapshot(()), attempts=Recorder())
    engine = A1Engine("unused", search=object())
    actual = engine.interpret_offer(reply, offer=offer, context=context)
    assert completed, "The configured model returned no successful physical response"
    # Malformed provider output can safely produce clarify in the application, but proves no language understanding.
    raw = json_reply(completed[-1])
    assert raw.get("intent") in {"confirm", "decline", "ask_details", "clarify", "related_question", "new_question"}
    assert raw.get("subject") in {"offered_question", "different", "uncertain"}
    assert raw.get("recipient") in {"quality_department", "different", "uncertain"}
    assert raw.get("commitment") in {"unconditional", "conditional", "none"}
    for field in ("evidence", "recipient_evidence"):
        assert isinstance(raw.get(field), str) and raw[field] in reply
    assert actual["intent"] == expected
