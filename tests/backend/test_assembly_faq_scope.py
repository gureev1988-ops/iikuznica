"""A semantic near-match never supplies identity evidence for a different case."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.assembly import faq_scope
from backend.assembly.engine import A1Engine
from backend.contracts.engine import AnswerResult, SourceSnapshot
from backend.db import connect
from backend.documents import list_fragments
from tests.backend.test_assembly_fast_answers import ACTOR, _publish_faq


@pytest.mark.parametrize(("stored", "incoming", "expected"), [
    ("Код тестового проекта 81ee47ea.", "Сколько нужно заложить по марже для 81EE47EA?", True),
    ("Код тестового проекта 81ee47ea.", "Какую маржу брать для проекта 37bb09ff?", False),
    ("Код тестового проекта 81ee47ea.", "Какую маржу брать на пресейле?", False),
    ("По сделке ТК-731 действует особое правило.", "Сделка тк‑731: какой лимит применить?", True),
    ("По сделке ТК-731 действует особое правило.", "Для ТК-731 или ТК-732 одинаковый лимит?", False),
    ("Код проекта ORION.", "Сколько закладывать для ORION?", True),
    ("Код проекта ORION.", "Сколько закладывать для ORIONA?", False),
    ("Ограничение проекта № 173.", "Для проекта 184 какой порог?", False),
    ("Код контракта 88117.", "Когда перечислят деньги по контракту 88117?", True),
    ("Порог для пресейла ИБ?", "Какова минимальная маржа на пресейле ИБ?", True),
])
def test_literal_identifiers(stored, incoming, expected):
    assert faq_scope.compatible_ids(stored, incoming, {}) is expected


def test_scope_can_come_from_expert_reply():
    answer = "Для сделки ОК-738 согласовано 18 процентов. Для остальных обращайтесь к куратору."
    stored = "Какой порог заложить? " + faq_scope.answer_entities(answer)
    assert not faq_scope.compatible_ids(stored, "Нужен порог для другой сделки ОК-740.", {})
    assert faq_scope.compatible_ids(stored, "Какую маржу требует сделка ОК-738?", {})


def test_changed_project_overrides_old_case_condition():
    assert not faq_scope.compatible_ids("Код проекта AL-9", "Теперь спрашиваю про AL-10", {"project": "AL-9"})
    assert faq_scope.compatible_ids("Код проекта AL-9", "Каков срок в этом случае?", {"project": "AL-9"})


def test_named_scope_does_not_accept_pronouns_or_other_names():
    equal = lambda left, right: False
    decision = {"faq_entity": "Ромашка", "request_entity": "Василёк"}
    assert not faq_scope.grounded_entity(decision, "Договор Ромашка", "Договор Василёк", equal)
    decision = {"faq_entity": "этого проекта", "request_entity": "этот проект"}
    assert not faq_scope.grounded_entity(decision, "Для этого проекта", "Про этот проект", equal)
    decision = {"faq_entity": "Недра", "request_entity": "Недра"}
    assert faq_scope.grounded_entity(decision, "Клиент «Недра»", "Какие сроки по «Недра»?", equal)
    assert not faq_scope.grounded_entity(decision, "Клиент «Недра»", "Клиент «Недрастрой»", equal)


def test_engine_retains_scoped_faq_only_for_grounded_question(dsn):
    stored = "Какой порог маржи на пресейле по проекту AR-719?"
    _publish_faq(dsn, stored)
    with connect(dsn) as conn:
        fragments = list_fragments(conn, ACTOR.id, kind="faq")
        card = str(conn.execute("SELECT id FROM faq_entries").fetchone()["id"])
    engine = A1Engine(dsn)
    gap = AnswerResult("escalate", "Передать вопрос в отдел качества?")
    answers = []
    status = "gap"

    def chat(*_):
        answers.append(1)
        return {"text": json.dumps({"id": card, "scope": "specific", "comparison": status})}

    def run(question, rewritten):
        req = SimpleNamespace(question=question, case_id=uuid4(), conditions={}, sources=SourceSnapshot(fragments))
        return engine._faq(req, SimpleNamespace(chat=chat), gap, rewritten)

    other = "Какой порог маржи нужно заложить по проекту AR-721?"
    assert run(other, stored) is None
    assert run("А без номера проекта?", stored) is None
    assert not answers
    good = "Какую минимальную маржу заложить на пресейле для AR-719?"
    assert run(good, good).citations[0].fragment_id == fragments[0].fragment_id
    status = "pending"
    assert run(good, good) is None
