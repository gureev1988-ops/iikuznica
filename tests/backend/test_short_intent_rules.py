"""Certify the action and exact source boundary of the additional short intents."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from backend.assembly import fast_answers
from backend.assembly.engine import A1Engine
from backend.contracts.engine import AnswerRequest, HistoryMessage, SourceFragment, SourceSnapshot
from eval.retrieval.extract import TITLES, clauses


TPS = "Кто назначает технического пресейл-специалиста (TPS) в Дирекции ИБ?"
ESTIMATE = "Дирекция ИБ. Какой срок подготовки бюджетной оценки для торгов?"
APPOINT = "Дирекция ИБ. В какой срок должны назначить руководителя проекта?"
SUBSTITUTE = "Кто замещает координатора проектов Дирекции ИБ на время отпуска?"


@pytest.fixture(scope="module")
def originals():
    fragments = []
    for doc, title in TITLES.items():
        document, revision = uuid4(), uuid4()
        fragments.extend(SourceFragment(document, revision, uuid5(revision, p["ref"]),
                                        p["text"], title, p["ref"], version=1)
                         for p in clauses(doc, title))
    return tuple(fragments)


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(fast_answers, "connect", lambda _: nullcontext())
    monkeypatch.setattr(fast_answers, "_official_originals", lambda *_: True)
    return A1Engine("unused", search=object()).fast_answers


def ask(text, originals, **changes):
    request = AnswerRequest(text, uuid4(), uuid4(), {}, (), datetime.now(UTC) + timedelta(seconds=10),
                            SourceSnapshot(originals))
    return replace(request, **changes)


@pytest.mark.parametrize(("question", "action", "expected", "evidence"), [
    (TPS, "escalate", "назначает ответственным за заявку пресейл-архитектора или TSS", [("PRE", "4.1")]),
    (ESTIMATE, "escalate", "до подачи тендерной заявки", [("PRE", "2.2"), ("PRE", "16.4"), ("PRE", "16.4.1")]),
    (APPOINT, "answer", "На этапе инициации", [("IB", "2.7"), ("IB", "2.2"), ("IB", "2.3")]),
    (SUBSTITUTE, "escalate", "не указан порядок замещения", [("IB", "2.1")]),
])
def test_short_intent_result(engine, originals, question, action, expected, evidence):
    request = ask(question, originals)
    result = engine.answer(request)
    assert isinstance(result, fast_answers.VerifiedAnswer)
    assert result.resolved_question == question and result.action == action and expected in result.text
    assert result.escalation_question == (question if action == "escalate" else None)
    allowed = {f.fragment_id: f for f in originals}
    assert [(allowed[c.fragment_id].title, allowed[c.fragment_id].anchor) for c in result.citations] == [
        (TITLES[doc], ref) for doc, ref in evidence
    ]
    assert all(c.quote in allowed[c.fragment_id].text for c in result.citations)
    assert len(engine._cache) == (1 if action == "answer" else 0)


@pytest.mark.parametrize("question", [
    "Кто назначает TPS Технической дирекции на проекте ИБ?",
    "Кто назначает TSS в Дирекции ИБ?",
    "Кто назначает исполнителя на пресейл ИБ при участии других ЦК?",
    "Какой срок подготовки бюджетной оценки для торгов?",
    "Какой срок актуализации расчёта для торгов в Дирекции ИБ?",
    "Когда назначают РП в Дирекции ИБ на пресейле, если это экзотика?",
    "Когда назначают РП в Дирекции ИБ на этапе реализации?",
    "Кто замещает координатора проектного офиса Дирекции ИБ на время отпуска?",
    TPS + " " + ESTIMATE,
])
def test_preserves_scope_for_nearby_intents(engine, originals, question):
    assert engine.answer(ask(question, originals)) is None


def test_context_or_applicable_faq_blocks_a_certified_gap(engine, originals, monkeypatch):
    history = (HistoryMessage("user", "Для предыдущего проекта обсуждали поставку."),)
    assert engine.answer(ask(ESTIMATE, originals, history=history)) is None
    assert engine.answer(ask(ESTIMATE, originals, conditions={"customer": "другой заказчик"})) is None
    faq = SourceFragment(uuid4(), uuid4(), uuid4(), "Уточнение срока.", kind="faq", version=1)

    def execute(*_):
        return SimpleNamespace(fetchall=lambda: [
            {"current_fragment_id": faq.fragment_id, "question_key": ESTIMATE, "conditions_key": "{}"},
        ])

    monkeypatch.setattr(fast_answers, "connect", lambda _: nullcontext(SimpleNamespace(execute=execute)))
    assert engine.answer(ask(ESTIMATE, (*originals, faq))) is None
