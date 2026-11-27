"""Direct replies require the applicable original and preserve FAQ scope."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from backend.assembly import fast_answers
from backend.assembly.corpus import seed
from backend.assembly.engine import A1Engine
from backend.assembly.fast_answers import VerifiedAnswer
from backend.assembly.quality_seed import ANSWER, QUESTION, seed_quality, training_original
from backend.contracts.api import MessageRequest
from backend.contracts.engine import AnswerRequest, SourceFragment, SourceSnapshot
from backend.db import connect
from backend.dialog import Actor, DialogueService
from backend.mailfaq.faq import _key
from backend.runtime import claim
from eval.retrieval.extract import TITLES, clauses


ESTIMATES = "Каковы рекомендуемые сроки выдачи бюджетной и точной оценки в ДБРЦТ?"
PACKAGE_TKP = ("Можно ли SS направить заказчику официальное ТКП без согласования с пресейл-архитектором "
               "при продаже пакетного предложения на пресейле в Дирекции ИБ?")
EARLY_START = "При каких условиях можно приступить к работам до подписания договора на проекте ДБРЦТ?"


@pytest.fixture
def revisions(tmp_path):
    training = tmp_path / "regulation-2.docx"
    training.write_bytes(training_original())
    snapshots = []
    ids = {doc: uuid4() for doc in TITLES}
    for db_version in (1, 2):
        fragments = []
        for doc, title in TITLES.items():
            version = db_version if doc == "DB" else 1
            revision = uuid5(ids[doc], str(version))
            path = training if version == 2 else None
            fragments.extend(SourceFragment(ids[doc], revision, uuid5(revision, p["ref"]), p["text"],
                                            title, p["ref"], version=version)
                             for p in clauses(doc, title, path))
        snapshots.append(tuple(fragments))
    return snapshots


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(fast_answers, "connect", lambda _: nullcontext())
    monkeypatch.setattr(fast_answers, "_official_originals", lambda *_: True)
    return A1Engine("unused", search=object()).fast_answers


def answer(engine, question, fragments, **changes):
    request = AnswerRequest(question, uuid4(), uuid4(), {}, (), datetime.now(UTC) + timedelta(seconds=10),
                            SourceSnapshot(fragments))
    return engine.answer(replace(request, **changes))


def evidence(result, fragments):
    assert isinstance(result, VerifiedAnswer) and result.action == "answer"
    visible = {f.fragment_id: f for f in fragments}
    cited = [visible[c.fragment_id] for c in result.citations]
    assert all(c.quote in f.text for c, f in zip(result.citations, cited))
    return [(f.anchor, f.version) for f in cited]


def test_estimate_deadlines_and_contractor_follow_active_revision(engine, revisions):
    older, newer = revisions
    for fragments, version, deadline in ((older, 1, 5), (newer, 2, 7), (older, 1, 5)):
        result = answer(engine, ESTIMATES, fragments)
        assert evidence(result, fragments) == [("1.6", version), ("1.7", version)]
        assert "3 рабочих дня" in result.text and f"{deadline} рабочих дней" in result.text
        assert "SLA на его поиск и согласование" in result.text
        assert "редакция" not in result.text
    edited = tuple(replace(f, text=f.text.replace("7 рабочих", "9 рабочих"))
                   if f.title == TITLES["DB"] and f.anchor == "1.6" else f for f in newer)
    assert answer(engine, ESTIMATES, edited) is None


def test_package_exception_keeps_general_coordination_rule_scoped(engine, revisions):
    fragments = revisions[1]
    result = answer(engine, PACKAGE_TKP, fragments)
    assert evidence(result, fragments) == [("2.3", 1)]
    assert "пакетного предложения" in result.text and "не требуется" in result.text
    assert result.escalation_question is None


def test_early_start_has_both_branches_and_full_followup(engine, revisions):
    fragments = revisions[1]
    result = answer(engine, EARLY_START, fragments)
    assert evidence(result, fragments) == [(f"8.{ref}", 2) for ref in range(4, 9)]
    assert "С гарантийным письмом" in result.text and "Без гарантийного письма" in result.text
    assert "письменное согласование" in result.text and "стоимость работ в рублях" in result.text
    assert "За 5 рабочих дней" in result.text and "в тот же день" in result.text
    assert "владельцу бюджета, АМ, SS и директору направления" in result.text


@pytest.mark.parametrize("question", [
    ESTIMATES.replace("в ДБРЦТ", ""),
    ESTIMATES.replace("в ДБРЦТ", "в прежней редакции регламента ДБРЦТ"),
    ESTIMATES + " Подрядчик возьмёт часть работ.",
    PACKAGE_TKP.replace("при продаже пакетного предложения", "при продаже непакетной услуги"),
    PACKAGE_TKP.replace("при продаже пакетного предложения", ""),
    PACKAGE_TKP.replace("при продаже пакетного предложения", "если статус пакетного предложения неизвестен"),
    EARLY_START.replace("ДБРЦТ", "Технической дирекции"),
    EARLY_START.replace("до подписания договора", "без согласования директора"),
    EARLY_START + " Письмо подписано, но смета изменилась.",
])
def test_adjacent_intents_remain_with_normal_engine(engine, revisions, question):
    assert answer(engine, question, revisions[1]) is None


def test_only_reviewed_seed_can_be_compared_locally(engine, revisions, monkeypatch):
    fragments = revisions[1]
    faq = SourceFragment(uuid4(), uuid4(), uuid4(), ANSWER, anchor="official-answer", kind="faq", version=1)
    all_fragments = (*fragments, faq)
    row = {"is_seed": True, "status": "active", "pending": [], "question_key": _key(QUESTION),
           "conditions_key": "{}", "current_fragment_id": faq.fragment_id}
    rows = [row]
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))
    monkeypatch.setattr(fast_answers, "connect", lambda _: nullcontext(conn))
    result = answer(engine, ESTIMATES, all_fragments)
    assert evidence(result, all_fragments) == [("1.6", 2), ("1.7", 2), ("official-answer", 1)]
    assert "7 рабочих дней" in result.text.split("FAQ Отдела качества")[0]
    assert "6 рабочих дней" in result.text.split("FAQ Отдела качества")[1]
    assert result.escalation_question == ESTIMATES
    assert not fast_answers.potential_faq(conn, ESTIMATES, {}, all_fragments)
    assert fast_answers.potential_faq(conn, QUESTION, {}, all_fragments)
    assert evidence(answer(engine, EARLY_START, all_fragments), all_fragments) == [
        (f"8.{ref}", 2) for ref in range(4, 9)
    ]
    row["pending"] = ["Уточните сторону"]
    assert answer(engine, ESTIMATES, all_fragments) is None
    row["pending"] = []
    assert answer(engine, ESTIMATES, (*fragments, replace(faq, text="Обновлено"))) is None
    other = replace(faq, fragment_id=uuid4())
    rows.append(dict(row, current_fragment_id=other.fragment_id, question_key=_key(ESTIMATES), is_seed=False))
    assert answer(engine, ESTIMATES, (*all_fragments, other)) is None


def test_current_rules_publish_with_real_training_revision_and_seed(dsn, tmp_path, monkeypatch):
    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    assembly = A1Engine(dsn)
    monkeypatch.setattr(assembly, "_answer", lambda *_: pytest.fail("Audited local rule used a model"))
    service = DialogueService(dsn=dsn, engine=assembly)
    actor = Actor("employee", frozenset({"employee"}), "employee@example.test")
    for question, versions in ((ESTIMATES, (2, 2, 1)), (PACKAGE_TKP, (1,)), (EARLY_START, (2,) * 5)):
        with connect(dsn) as conn:
            turn = service.admit(conn, actor, uuid4(), MessageRequest(text=question, client_message_id=str(uuid4())))
        service.answer_job(claim(dsn, "interactive"))
        with connect(dsn) as conn:
            turn_row = service.get_turn(conn, actor, turn.turn_id)
            result = turn_row["result"]
            assert turn_row["status"] == "completed" and result["action"] == "answer"
            assert tuple(
                conn.execute("SELECT version FROM source_revisions WHERE id=%s", (source["revision_id"],))
                .fetchone()["version"] for source in result["sources"]
            ) == versions
            offers = conn.execute("SELECT payload->>'question' AS target FROM offers WHERE turn_id=%s",
                                  (turn.turn_id,)).fetchall()
            assert offers == ([{"target": question}] if question == ESTIMATES else [])
