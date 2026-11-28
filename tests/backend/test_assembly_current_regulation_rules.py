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
from backend.contracts.engine import AnswerRequest, HistoryMessage, SourceFragment, SourceSnapshot
from backend.db import connect
from backend.dialog import Actor, DialogueService
from backend.mailfaq.faq import _key
from backend.runtime import claim
from eval.retrieval.extract import TITLES, clauses


ESTIMATES = "Каковы рекомендуемые сроки выдачи бюджетной и точной оценки в ДБРЦТ?"
PACKAGE_TKP = ("Можно ли SS направить заказчику официальное ТКП без согласования с пресейл-архитектором "
               "при продаже пакетного предложения на пресейле в Дирекции ИБ?")
EARLY_START = "При каких условиях можно приступить к работам до подписания договора на проекте ДБРЦТ?"
KICKOFF = "Кого нужно пригласить на установочную встречу?"


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


def test_kickoff_asks_for_the_directorate_only_on_audited_sources(engine, revisions):
    fragments = revisions[1]
    result = answer(engine, KICKOFF, fragments)
    assert isinstance(result, VerifiedAnswer) and result.action == "clarify"
    assert result.clarification_key == "directorate" and not result.citations
    assert [option.text for option in result.options] == ["ДБРЦТ", "Дирекция ИБ", "Техническая дирекция", "Не знаю"]
    assert answer(engine, KICKOFF, fragments[1:]) is None
    assert answer(engine, KICKOFF + " И какие собрать документы?", fragments) is None
    assert answer(engine, "Кого пригласить на установочную встречу в Дирекции ИБ?", fragments) is None


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


@pytest.mark.parametrize("estimate_question,start_question", [
    (ESTIMATES, EARLY_START),
    ("Сколько рабочих дней у ДБРЦТ обычно занимают две оценки — точная и бюджетная?",
     "При каких условиях команда ДБРЦТ может начать работы, пока договор с заказчиком ещё не подписан?"),
    ("В какие сроки в ДБРЦТ следует подготовить точную и бюджетную оценки?",
     "Когда в ДБРЦТ разрешается начать работы по проекту до подписания договора?"),
])
def test_only_reviewed_seed_can_be_compared_locally(engine, revisions, monkeypatch, estimate_question, start_question):
    fragments = revisions[1]
    faq = SourceFragment(uuid4(), uuid4(), uuid4(), ANSWER, anchor="official-answer", kind="faq", version=1)
    all_fragments = (*fragments, faq)
    row = {"is_seed": True, "status": "active", "pending": [], "question_key": _key(QUESTION),
           "conditions_key": "{}", "current_fragment_id": faq.fragment_id}
    rows = [row]
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))
    monkeypatch.setattr(fast_answers, "connect", lambda _: nullcontext(conn))
    result = answer(engine, estimate_question, all_fragments)
    assert evidence(result, all_fragments) == [("1.6", 2), ("1.7", 2), ("official-answer", 1)]
    assert "7 рабочих дней" in result.text.split("FAQ Отдела качества")[0]
    assert "6 рабочих дней" in result.text.split("FAQ Отдела качества")[1]
    assert result.escalation_question == estimate_question
    assert not fast_answers.potential_faq(conn, estimate_question, {}, all_fragments)
    assert fast_answers.potential_faq(conn, QUESTION, {}, all_fragments)
    assert evidence(answer(engine, start_question, all_fragments), all_fragments) == [
        (f"8.{ref}", 2) for ref in range(4, 9)
    ]
    row["pending"] = ["Уточните сторону"]
    assert answer(engine, estimate_question, all_fragments) is None
    row["pending"] = []
    assert answer(engine, estimate_question, (*fragments, replace(faq, text="Обновлено"))) is None
    other = replace(faq, fragment_id=uuid4())
    rows.append(dict(row, current_fragment_id=other.fragment_id, question_key=_key(estimate_question), is_seed=False))
    assert answer(engine, estimate_question, (*all_fragments, other)) is None


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

    with connect(dsn) as conn:
        turn = service.admit(conn, actor, uuid4(), MessageRequest(text=KICKOFF, client_message_id=str(uuid4())))
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        result = service.get_turn(conn, actor, turn.turn_id)
        assert result["status"] == "completed" and result["result"]["action"] == "clarify"
        assert len(result["clarifications"]) == 1 and result["offers"] == []


@pytest.mark.parametrize("question,baseline", [
    ("В какие сроки следует подготовить точную и бюджетную оценки в ДБРЦТ?", ESTIMATES),
    ("За сколько времени в ДБРЦТ рекомендуют предоставлять бюджетную и точную оценки?", ESTIMATES),
    ("На пресейле Дирекции ИБ может ли TSS передать заказчику официальное ТКП без согласования с ПА "
     "при продаже пакетного предложения?", PACKAGE_TKP),
    ("В рамках пакетного предложения SS вправе направить клиенту официальное ТКП без согласования с ПА "
     "в пресейле ИБ?", PACKAGE_TKP),
    ("В ДБРЦТ на каких условиях допускается приступить к работам по проекту до подписания договора?", EARLY_START),
    ("Заказчик просит ДБРЦТ начать работы до того, как клиент подпишет договор. "
     "При каких условиях это допустимо?", EARLY_START),
    ("Кого позвать, если готовим установочное совещание по проекту?", KICKOFF),
    ("Организуем установочное совещание. Кто должен на нем присутствовать?", KICKOFF),
])
def test_scope_phrases_preserve_the_audited_answer(question, baseline, engine, revisions):
    for fragments in revisions:
        result = answer(engine, question, fragments)
        assert result == replace(answer(engine, baseline, fragments), resolved_question=question)


def test_competition_notification_uses_complete_authorized_clause(engine, revisions):
    text = "После выигрыша конкурса технической дирекцией каким образом SS оповестить TPS о победе, что сообщить?"
    fragments = revisions[1]
    result = answer(engine, text, fragments)
    assert evidence(result, fragments) == [("8.1", 1)]
    assert all(part in result.text for part in ("SS", "TPS", "в Заявке на пресейл", "номер и ссылку",
                                               "с итоговым договором"))
    assert answer(engine, text, fragments[1:]) is None


@pytest.mark.parametrize("question", [
    "В ДБРЦТ за сколько рабочих дней рекомендуют делать бюджетную и ориентировочную оценки?",
    "За сколько дней рекомендуют делать бюджетную и точную оценки в ДБРЦТ при наличии подрядчика?",
    "Клиент просит ДБРЦТ не приступать к работам до подписания договора. Какие условия нужно выполнить?",
    "Когда ДБРЦТ допускается начать работы до подписания дополнительного соглашения?",
    "В ДБРЦТ на этапе реализации когда разрешается начать работы до подписания договора?",
    "Может ли заказчик на пресейле ИБ передать SS официальное ТКП на пакетное предложение без согласования с ПА?",
    "SS в пресейле ИБ вправе не выдавать официальное ТКП на пакетное предложение без согласования с ПА?",
    "В рамках пакетного предложения ИБ и ДБРЦТ SS вправе выдать официальное ТКП без согласования с ПА?",
    "В пресейле ИБ SS вправе выдать официальное ТКП на пакетное предложение без согласования с юристом?",
    "Кого пригласить, если организую внутреннюю установочную встречу по проекту?",
    "Готовлю установочную встречу Дирекции ИБ. Кто должен на ней присутствовать?",
    "Готовлю установочную встречу. Кто не должен на ней присутствовать?",
    "Техническая дирекция не выиграла конкурс. Как продавцу решений известить TPS и что указать в сообщении?",
    "Техническая дирекция выиграла конкурс. Как TPS уведомить продавца решений и что указать в сообщении?",
    "Техническая дирекция выиграла конкурс. Как продавцу решений известить TPS и что указать для другого этапа?",
])
def test_unreviewed_qualifier_keeps_normal_routing(question, engine, revisions):
    assert answer(engine, question, revisions[1]) is None


def test_positive_approval_question_requires_a_negative_answer(engine, revisions):
    query = ("На пресейле ИБ SS продаёт пакетное предложение. "
             "Требуется ли согласовать с ПА официальное ТКП до отправки заказчику?")
    result = answer(engine, query, revisions[1])
    assert evidence(result, revisions[1]) == [("2.3", 1)]
    assert result.text.startswith("Нет.") and "без согласования" in result.text
    for other in (query.replace("SS", "АМ"), query.replace("пакетное", "непакетное"),
                  query.replace("официальное", "черновое"), query.replace("с ПА", "с директором")):
        assert answer(engine, other, revisions[1]) is None


def test_early_start_is_a_new_operation_only_after_a_completed_estimate(engine, revisions):
    fragments = revisions[1]
    query = "В ДБРЦТ можно ли начать работы до подписания договора?"
    estimate = answer(engine, ESTIMATES, fragments)
    history = (HistoryMessage("user", ESTIMATES), HistoryMessage("assistant", estimate.text, ESTIMATES, ESTIMATES, "answer"))
    result = answer(engine, query, fragments, history=history)
    assert evidence(result, fragments) == [(f"8.{i}", 2) for i in range(4, 9)]
    assert "Без гарантийного письма" in result.text
    for other in (query.replace("в ДБРЦТ", "вне ДБРЦТ").replace("В ДБРЦТ", "Вне ДБРЦТ"),
                  query.replace("до подписания договора", "до подписания допсоглашения"),
                  query.replace("начать", "не начать"), query.replace("договора?", "договора с подрядчиком?")):
        assert answer(engine, other, fragments, history=history) is None
    for changes in ({"conditions": {"directorate": "ИБ"}}, {"clarification_count": 1},
                    {"clarification": {"value": "Да"}}, {"history": (*history, HistoryMessage("user", "Но письмо уже есть"))}):
        assert answer(engine, query, fragments, **({"history": history} | changes)) is None


def test_directorate_followup_keeps_tender_and_requires_the_prior_answer_and_active_sources(engine, revisions):
    query = "Какой срок подготовки бюджетной оценки для торгов у Технической дирекции?"
    for fragments in revisions:
        previous = answer(engine, query, fragments)
        history = (HistoryMessage("user", query), HistoryMessage("assistant", previous.text, query, query, "answer"))
        result = answer(engine, "А в ДБРЦТ?", fragments, history=history)
        assert [anchor for anchor, _ in evidence(result, fragments)] == ["2.4", "1.6", "1.7"]
        assert "нельзя использовать для подачи в конкурс" in result.text and "RFI" in result.text
        assert "3 рабочих дня" in result.text and "2 недели" not in result.text
        assert result.checked_questions == (query, result.resolved_question)
        assert "для торгов" in result.resolved_question
        assert answer(engine, "А в ДБРЦТ?", fragments, history=(history[0], replace(history[1], text="Иная операция"))) is None
        assert answer(engine, "А в ДБРЦТ?", fragments, history=(history[0], replace(history[1], action="clarify"))) is None
        other = query.replace("торгов", "контракта")
        assert answer(engine, "А в ДБРЦТ?", fragments, history=(HistoryMessage("user", other),
                      replace(history[1], question=other, resolved=other))) is None
        edited = tuple(replace(f, text=f.text.replace("RFI", "RFP")) if f.title == TITLES["DB"] and f.anchor == "2.4"
                       else f for f in fragments)
        assert answer(engine, "А в ДБРЦТ?", edited, history=history) is None
