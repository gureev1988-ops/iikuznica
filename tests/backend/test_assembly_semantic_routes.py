from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.assembly.navigation import document_answer
from backend.assembly.references import resolve_reference
from backend.assembly.smtp_gateway import BOT, DOMAIN, QUALITY
from backend.contracts.api import MessageRequest
from backend.contracts.engine import AnswerResult, ClarificationOption, SourceFragment, SourceSnapshot
from backend.db import connect
from backend.dialog import Actor, DialogueService
from backend.dialog.clarifications import match_options, numbered, offer_decision
from backend.dialog.service import DialogueError
from backend.mailfaq import MailConfig, MailService
from backend.mailfaq.interpret import verified_reply
from backend.mailfaq.message import ParsedMail
from backend.runtime import run_once


def fragment(title, text, anchor="1.2"):
    return SourceFragment(uuid4(), uuid4(), uuid4(), text, title, anchor, version=2)


def test_open_documents_uses_current_catalog_and_numbered_choice():
    presale = fragment("Пресейловая деятельность Дирекции ИБ", "Правило пресейла.")
    project = fragment("Проектная деятельность Дирекции ИБ", "Работа с проектом.")
    project_two = fragment("Проектная деятельность ДБРЦТ", "Порядок работ.")
    req = SimpleNamespace(question="Где посмотреть регламент по пресейлу дирекции ИБ, пункт 1.2?",
                          sources=SourceSnapshot((presale, project, project_two)),
                          conditions={}, clarification=None, history=())
    answer = document_answer(req)
    assert "редакция 2" in answer.text and answer.citations[0].fragment_id == presale.fragment_id
    assert answer.citations[0].claim == "Открыть регламент"
    req.question = "Где регламент по проектной деятельности?"
    ask = document_answer(req)
    assert {o.text for o in ask.options if o.id != "unknown"} == {project.title, project_two.title}
    req.clarification = {"key": ask.clarification_key, "value": project_two.title}
    assert document_answer(req).citations[0].document_id == project_two.document_id
    req.question, req.clarification = "Где найти регламент о закупке приборов?", None
    assert document_answer(req).action == "escalate"
    req.question = "Кто назначает руководителя проекта?"
    assert document_answer(req) is None


def test_cross_reference_requires_grounded_origin_and_destination():
    origin = fragment("Действия", "Срок установлен инструкцией о перевозке.")
    target = fragment("Инструкция о перевозке", "Перевозка занимает 3 дня.")
    refs = {("act", "1"): origin, ("instruction", "2"): target}
    corpus = {k: {"doc": k[0], "ref": k[1], "text": f.text} for k, f in refs.items()}

    def lookup(q):
        return [("act", "1")], list(corpus.values())

    import json
    proposal = dict(doc="act", ref="1", quote=origin.text, name="инструкцией о перевозке",
                    missing="Каков срок перевозки?", target="instruction")
    proof = dict(ref="2", quote=target.text, answer="Срок перевозки — 3 дня.")
    calls = SimpleNamespace(chat=lambda _, body, stage: {"text": json.dumps(
        proposal if stage == "document-reference" else proof)})
    def run():
        return resolve_reference("Когда привезут прибор?", lookup, corpus, refs, calls, {"sources": [], "action": "escalate"})
    assert [r.fragment_id for r in run().citations] == [origin.fragment_id, target.fragment_id]
    proposal["target"] = None
    assert run().action == "escalate" and run().citations[0].quote == origin.text
    proposal["quote"] = "Этого в источнике нет."
    assert run() is None


@pytest.mark.parametrize("text,id_", [("2", "impl"), ("Уже реализуем проект", "impl"),
    ("Мы уже 2 недели на реализации", "impl"), ("5", "unknown"), ("Этого не вижу в карточке", "unknown")])
def test_number_uses_list_order_and_keeps_free_text(text, id_):
    options = [dict(id=k, text=v) for k, v in [
        ("pre", "Пресейл"), ("impl", "Реализация проекта"), ("risk", "Управление рисками"),
        ("new", "Запрос на новую услугу"), ("unknown", "Не знаю")]]
    assert match_options(text, options)[0]["id"] == id_
    assert "5. Не знаю" in numbered("Какой этап?", options)
    assert match_options("7", options) == []
    assert match_options("7", []) is None
    assert offer_decision("Нет, не отправляй") == "declined"
    assert offer_decision("Да, передай") == "confirmed"
    assert offer_decision("Может быть, передам позже") is None


def test_semantic_mail_quotes_must_be_from_the_correct_message():
    parsed = ParsedMail(None, QUALITY, True, (), (), "substantive")
    answer, later = "Доставляем за 3 дня.", "Согласование ещё проверяем."
    partial = verified_reply(parsed, {"kind": "partial", "answer": [answer], "status": [later]},
                             f"Здравствуйте! {answer} {later}", [])
    assert partial.public_text == answer and partial.pending == (later,)
    final = "Согласование отдельное."
    merged = verified_reply(parsed, {"kind": "substantive", "answer": [final], "retain": [answer]},
                            final, [answer])
    assert merged.public_text == f"{answer}\n\n{final}"
    scoped = "Для участка Зета срок составляет 4 дня, кроме выходных."
    assert verified_reply(parsed, {"kind": "substantive", "answer": ["срок составляет 4 дня"]},
                          scoped, []).public_text == scoped
    assert verified_reply(parsed, {"kind": "substantive", "answer": [answer]}, final, []).kind == "unrecognized"
    assert verified_reply(parsed, {"kind": "status", "status": [later]}, later, []).kind == "status"


def test_number_and_explicit_mail_consent_freeze_only_the_current_case(dsn, monkeypatch):
    identity = uuid4()
    actor = Actor(f"guest:{identity}", frozenset({"employee"}), f"guest-{identity.hex}@{DOMAIN}", "guest")
    conversation = uuid4()
    config = MailConfig(sender=BOT, recipient=QUALITY, reply_to=BOT, trusted_senders=(QUALITY,))

    class Engine:
        def answer(self, request):
            if request.question == "Старый личный вопрос":
                return AnswerResult("out_of_scope", "Частная тема.")
            if not request.conditions:
                return AnswerResult("clarify", "Назовите этап.", options=(
                    ClarificationOption("intake", "Приём"), ClarificationOption("review", "Проверка")),
                    clarification_key="stage")
            assert request.clarification["value"] == "Проверка" and request.clarification["original"] == "2"
            return AnswerResult("escalate", "Для проверки срок не указан. Передать вопрос?")

    service = DialogueService(dsn=dsn, engine=Engine(), mail_config=config)

    def send(text, client_id, **fields):
        with connect(dsn) as conn:
            return service.admit(conn, actor, conversation, MessageRequest(
                text=text, client_message_id=client_id, **fields))

    def answer():
        assert run_once(dsn, "interactive", {"answer_turn": service.answer_job})

    send("Старый личный вопрос", "previous")
    answer()
    first = send("Каков срок проверки отчёта?", "ask")
    answer()
    with connect(dsn) as conn:
        action = service.open_actions(conn, actor, conversation)["clarifications"][0]
    selection = dict(case_id=first.case_id, clarification_id=action["id"], clarification_version=1)
    with pytest.raises(DialogueError, match="clarification_choice_invalid"):
        send("99", "invalid", **selection)
    send("2", "select", **selection)
    answer()
    consent = send("Да, передай", "consent", case_id=first.case_id)
    assert send("Да, передай", "consent", case_id=first.case_id) == consent
    with connect(dsn) as conn:
        result = service.get_turn(conn, actor, consent.turn_id)["result"]
        assert result["case_number"] == 2
        request = conn.execute("SELECT * FROM mail_requests").fetchone()
        out = conn.execute("SELECT * FROM mail_outbound").fetchone()
        assert result["mail_request_id"] == str(request["id"])
        for expected in ("1. Вопрос от сотрудника", "2. Почему направлено", "3. Диалог до отправки",
                         "2. Проверка", "Сотрудник: 2", "Да, передай", "Назовите этап. — Проверка"):
            assert expected in out["body"]
        assert "Старый личный вопрос" not in out["body"]
    mail = MailService(dsn, config)
    assert run_once(dsn, "background", mail.handlers, active_kinds=mail.handlers)
    initial = mail.simulator.sent[0]
    assert initial.subject == "Обращение ОК2"
    responses = iter([dict(kind="status", status=["Передали владельцу."]),
                      dict(kind="partial", answer=["Подача через портал."], pending=["Какая категория?"]),
                      dict(kind="substantive", retain=["Подача через портал."], answer=["Для категории А — 4 дня."])])
    def classify(dsn, job, request, raw, parsed, history):
        from email import policy
        from email.parser import BytesParser
        body = BytesParser(policy=policy.default).parsebytes(raw).get_body().get_content()
        return verified_reply(parsed, next(responses), body, [p.get("public_text","") for p in history])
    monkeypatch.setattr("backend.mailfaq.interpret.interpret", classify)
    monkeypatch.setattr("backend.assembly.quality.prepare_review", lambda *args: None)
    for i, body in enumerate(["Передали владельцу.", "Подача через портал. Какая категория?", "Для категории А — 4 дня."]):
        mail.simulator.reply(initial, body, message_id=f"<part-{i}@assembly.test>")
        assert mail.poll_inbox() == 1
        assert run_once(dsn, "background", mail.handlers, active_kinds=mail.handlers)
        with connect(dsn) as conn:
            assert conn.execute("SELECT count(*) n FROM faq_entries").fetchone()["n"] == (i == 2)
            assert conn.execute("SELECT state FROM mail_requests").fetchone()["state"] == (
                "waiting_reply", "partial", "resolved")[i]
    assert mail.poll_inbox() == 0


def test_conflict_review_compares_the_same_action_and_survives_provider_outage():
    from backend.assembly.engine import A1Engine
    from backend.contracts.engine import EngineUnavailable
    from backend.assembly.competing_rules import competition

    first = fragment("Положение Север", "Для перевозки прибора срок согласования составляет 3 дня.")
    second = fragment("Положение Юг", "Для перевозки прибора срок согласования составляет 7 дней.")
    unrelated = fragment("Выдача ключей", "Для ворот нужен ключ начальника смены.")
    refs = {("n", "1"): first, ("s", "2"): second, ("k", "3"): unrelated}
    entries = {k: dict(doc=k[0], ref=k[1], text=f.text, section=f.title) for k, f in refs.items()}
    lookup = lambda text: (list(entries), list(entries.values()))
    question = "Каков срок согласования перевозки прибора?"
    def unavailable(*args):
        raise EngineUnavailable("offline")
    calls = SimpleNamespace(chat=unavailable)
    engine = A1Engine(None, search=object())
    result = engine._regulation_conflict(
        question, calls, lookup, entries, refs, [entries["n", "1"]])
    assert result.action == "escalate" and len(result.citations) == 2
    assert "Не удалось подтвердить" in result.text
    assert {c.fragment_id for c in result.citations} == {first.fragment_id, second.fragment_id}
    assert not competition(question, {("n","1"):entries["n","1"], ("k","3"):entries["k","3"]}, set())

    # An uncited result must still go through normal ranking if the lexical pool
    # contains a possible conflict. Only a pool without pairs skips inference.
    lookup.lexical_pool = lambda text: list(entries.values())
    assert engine._regulation_conflict(question, calls, lookup, entries, refs, []) == result

    def no_rerank(text):
        raise AssertionError("The lexical pool already rules out a conflict")

    no_rerank.lexical_pool = lambda text: [entries["n", "1"], entries["k", "3"]]
    assert engine._regulation_conflict(question, calls, no_rerank, entries, refs, []) is None


def test_compound_document_request_keeps_the_independent_question():
    from backend.assembly.navigation import split_document_request
    link = "Пришлите сам регламент про пресейл ИБ."
    question = "По моему запросу ресурс-менеджер не ответил уже рабочий день — к кому могу подключиться?"
    assert split_document_request(link + " " + question) == (link, question)


def test_common_mail_does_not_publish_employee_only_evidence(dsn):
    from psycopg.types.json import Jsonb
    from backend.contracts.fixtures import seed_fixture_sources
    from backend.mailfaq.shared_context import shared_context
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
        ref = conn.execute("SELECT f.id AS fragment_id,s.id AS document_id FROM source_fragments f "
                           "JOIN source_revisions r ON r.id=f.revision_id JOIN sources s ON s.id=r.source_id "
                           "WHERE s.acl_actor_ids IS NULL LIMIT 1").fetchone()
        conn.execute("UPDATE sources SET acl_actor_ids=ARRAY['owner'] WHERE id=%s", (ref["document_id"],))
        conversation = conn.execute("INSERT INTO conversations(owner_id) VALUES ('owner') RETURNING id").fetchone()["id"]
        refs = [{"fragment_id": str(ref["fragment_id"])}]
        case = conn.execute("INSERT INTO cases(conversation_id,condition_sources) VALUES (%s,%s) RETURNING id",
                            (conversation,Jsonb({"special":refs}))).fetchone()["id"]
        result = {"action":"escalate","text":"Секретный пункт","sources":refs}
        turn = conn.execute("INSERT INTO turns(conversation_id,case_id,request_id,question,result) "
                            "VALUES (%s,%s,'ask','Как согласовать?',%s) RETURNING id",
                            (conversation,case,Jsonb(result))).fetchone()["id"]
        history = [dict(case_id=case,turn_id=turn,role="user",text="Как согласовать?"),
                   dict(case_id=case,turn_id=turn,role="assistant",text=result["text"])]
        question, conditions, transcript, result = shared_context(conn,case,turn,history,
            "Секретный пункт", {"special":"Секретный пункт","stage":"пресейл"})
        assert question == "Как согласовать?" and not result
        assert conditions == {"stage":"пресейл"}
        assert "Секретный пункт" not in transcript[1]["text"]
