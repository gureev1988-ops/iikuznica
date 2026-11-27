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
    ("Тестовый проект iishka.", "Какой порог по учебному проекту «IISHKA»?", True),
    ("Тестовый проект iishka.", "Сколько закладывать для iishka?", True),
    ("Тестовый проект iishka.", "Какую маржу брать на пресейле?", False),
    ("Тестовый проект iishka.", "Какую маржу брать для проекта iishka-west?", False),
    ("Тестовый проект iishka.", "Для iishka и проекта Orion один порог?", False),
    ("Для проекта «Тестовый проект iishka».", "Какой порог для учебного проекта IISHKA?", True),
    ("Для проекта «Тестовый проект iishka».", "Какова маржа для технического проекта?", False),
    ("Порог по проекту пресейла ИБ?", "Какова минимальная маржа на пресейле ИБ?", True),
    ("Порог для пресейла ИБ?", "Какова минимальная маржа на пресейле ИБ?", True),
])
def test_literal_identifiers(stored, incoming, expected):
    assert faq_scope.compatible_ids(stored, incoming, {}) is expected


def test_scope_can_come_from_expert_reply():
    answer = "Для сделки ОК-738 согласовано 18 процентов. Для остальных обращайтесь к куратору."
    stored = "Какой порог заложить? " + faq_scope.answer_entities(answer)
    assert not faq_scope.compatible_ids(stored, "Нужен порог для другой сделки ОК-740.", {})
    assert faq_scope.compatible_ids(stored, "Какую маржу требует сделка ОК-738?", {})
    answer = "Только для учебного проекта iishka действует порог 18 процентов."
    scope = faq_scope.answer_entities(answer)
    assert not faq_scope.compatible_ids(scope, "Какой порог на пресейле?", {})
    assert faq_scope.compatible_ids(scope, "Сколько закладывать для iishka?", {})


def test_changed_project_overrides_old_case_condition():
    assert not faq_scope.compatible_ids("Код проекта AL-9", "Теперь спрашиваю про AL-10", {"project": "AL-9"})
    assert faq_scope.compatible_ids("Код проекта AL-9", "Каков срок в этом случае?", {"project": "AL-9"})


@pytest.mark.parametrize("document", ["договору", "смете"])
def test_named_document_within_project(document):
    project = "для проекта «Тестовый проект iishka»"
    title = "Поставка сетевого оборудования от 28 ноября 2026 года в 12:20:07 МСК"
    terms = f"{project} по {document} «{title}»"
    other_terms = terms.replace("12:20:07", "12:20:08")
    stored = faq_scope.answer_entities(f"Минимальная маржа {terms} составляет 18 процентов.")
    assert faq_scope.compatible_ids(stored, f"Какова маржа {terms}?", {})
    assert not faq_scope.compatible_ids(stored, f"Какова маржа {other_terms}?", {"document": title})
    assert not faq_scope.compatible_ids(stored, f"Какова маржа {project}?", {})
    assert not faq_scope.compatible_ids(stored, terms.replace(project, "для другого проекта"), {})
    assert not faq_scope.compatible_ids(project, f"Какова маржа {terms}?", {})
    assert faq_scope.compatible_ids("Какой порог на пресейле ИБ?", f"Какова маржа {terms}?", {})
    assert not faq_scope.compatible_ids(stored, terms.replace(project, "для проекта iishka-west"), {})
    assert faq_scope.compatible_ids(stored, f"Какова маржа {project}?", {"document": title})


def test_named_scope_does_not_accept_pronouns_or_other_names():
    equal = lambda left, right: False
    decision = {"faq_entity": "Ромашка", "request_entity": "Василёк"}
    assert not faq_scope.grounded_entity(decision, "Договор Ромашка", "Договор Василёк", equal)
    decision = {"faq_entity": "этого проекта", "request_entity": "этот проект"}
    assert not faq_scope.grounded_entity(decision, "Для этого проекта", "Про этот проект", equal)
    decision = {"faq_entity": "Недра", "request_entity": "Недра"}
    assert faq_scope.grounded_entity(decision, "Клиент «Недра»", "Какие сроки по «Недра»?", equal)
    assert not faq_scope.grounded_entity(decision, "Клиент «Недра»", "Клиент «Недрастрой»", equal)


@pytest.mark.parametrize(("project", "other_project"), [("AR-719", "AR-721"), ("iishka", "Orion")])
def test_engine_retains_scoped_faq_only_for_grounded_question(dsn, project, other_project):
    stored = f"Какой порог маржи на пресейле по проекту {project}?"
    _publish_faq(dsn, stored)
    with connect(dsn) as conn:
        fragments = list_fragments(conn, ACTOR.id, kind="faq")
    engine = A1Engine(dsn)
    gap = AnswerResult("escalate", "Передать вопрос в отдел качества?")
    answers = []
    status = "gap"

    def chat(*_):
        answers.append(1)
        return {"text": json.dumps({"id": 1, "scope": "specific", "comparison": status})}

    def run(question, rewritten):
        req = SimpleNamespace(question=question, case_id=uuid4(), conditions={}, sources=SourceSnapshot(fragments))
        return engine._faq(req, SimpleNamespace(chat=chat), gap, rewritten)

    other = f"Какой порог маржи нужно заложить по проекту {other_project}?"
    assert run(other, stored) is None
    assert run("А без номера проекта?", stored) is None
    assert not answers
    good = f"Какую минимальную маржу заложить на пресейле для {project}?"
    assert run(good, good).citations[0].fragment_id == fragments[0].fragment_id
    status = "pending"
    assert run(good, good) is None


def test_engine_checks_estimate_before_semantic_ranking(dsn):
    general = "Какая маржа нужна для проекта «Тестовый проект iishka»"
    for suffix in ("?", " по смете «Сеть филиалов»?", " по смете «Поставка серверов»?"):
        _publish_faq(dsn, general + suffix)
    with connect(dsn) as conn:
        fragments = list_fragments(conn, ACTOR.id, kind="faq")
    engine = A1Engine(dsn)
    candidates = []

    def chat(role, body, purpose):
        candidates.extend(json.loads(body["messages"][-1]["content"])["sources"])
        return {"text": '{"id":1,"scope":"specific","comparison":"gap"}'}

    def run(question):
        req = SimpleNamespace(question=question, case_id=uuid4(), conditions={}, sources=SourceSnapshot(fragments))
        return engine._faq(req, SimpleNamespace(chat=chat), AnswerResult("escalate", "Уточнить?"), general)

    assert run(general + " по смете «Поддержка клиентов»?") is None
    assert not candidates
    assert run("Какой порог для iishka по смете «Поставка серверов»?").citations
    assert len(candidates) == 1 and "поставка серверов" in candidates[0]["question"]
