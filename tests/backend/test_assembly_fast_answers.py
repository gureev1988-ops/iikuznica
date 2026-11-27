"""Fast replies must remain equivalent to the reviewed source and authorization scope."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from backend.assembly import fast_answers
from backend.assembly.corpus import seed
from backend.assembly.engine import A1Engine
from backend.assembly.fast_answers import VerifiedAnswer, VerifiedAnswers
from backend.contracts.api import MessageRequest
from backend.contracts.engine import (
    AnswerRequest, AnswerResult, ClarificationOption, HistoryMessage, SourceFragment, SourceSnapshot,
)
from backend.db import connect, lock_source_catalog
from backend.dialog import Actor, DialogueService
from backend.dialog.service import EvidenceChanged
from backend.mailfaq import MailConfig
from backend.mailfaq.faq import publish_faq
from backend.runtime import claim
from eval.retrieval.extract import TITLES, clauses


IB_QUESTION = "В какой срок в Дирекции информационной безопасности назначают руководителя проекта при инициации?"
DB_QUESTION = "Кто и в какой срок назначают РП на этапе инициации в ДБРЦТ?"
PRE_QUESTION = ("На пресейле в Дирекции ИБ заказчик уже выбрал оборудование, запросил только расчёт поставки "
                "и явно отказался от всех услуг. Кому BDM направляет ТКП?")
TECH_QUESTION = "Какой срок подготовки бюджетной оценки для торгов у Технической дирекции?"
TPS_QUESTION = "Кто назначает TPS в Дирекции ИБ?"
PM_QUESTION = "В какой срок должны назначить руководителя проекта?"
ACTOR = Actor("employee", frozenset({"employee"}), "employee@example.test")


@pytest.fixture(scope="module")
def fragments():
    result = []
    for doc, title in TITLES.items():
        document, revision = uuid4(), uuid4()
        result.extend(SourceFragment(document, revision, uuid5(revision, passage["ref"]),
                                     passage["text"], title, passage["ref"], version=1)
                      for passage in clauses(doc, title))
    return tuple(result)


def question(text, fragments, **kwargs):
    req = AnswerRequest(text, uuid4(), uuid4(), {}, (), datetime.now(UTC) + timedelta(seconds=20),
                        SourceSnapshot(fragments))
    return replace(req, **kwargs)


@pytest.fixture
def engine(monkeypatch):
    engine = A1Engine("unused", search=object())
    check = []
    faq_rows = {}

    @contextmanager
    def connection(_):
        def execute(statement, args):
            if "assembly_originals" in statement:
                check.append(tuple(args[0]))
                rows = originals
            else:
                assert "faq_entries" in statement
                rows = [faq_rows[fragment_id] for fragment_id in args[0] if fragment_id in faq_rows]
            return SimpleNamespace(fetchall=lambda: rows)
        yield SimpleNamespace(execute=execute)

    originals = []
    monkeypatch.setattr(fast_answers, "connect", connection)
    return engine, check, originals, faq_rows


def allow_originals(rows, fragments):
    dimensions = {(fragment.title, fragment.document_id, fragment.revision_id) for fragment in fragments}
    rows[:] = [
        dict(external_id=doc, document_id=document, revision_id=revision, sha256=fast_answers.ORIGINALS[doc][0])
        for title, document, revision in dimensions
        if (doc := fast_answers.DOC_BY_TITLE.get(title))
    ]


@pytest.mark.parametrize(("text", "expected", "anchors"), [
    (IB_QUESTION, "1 рабочего дня", ["2.7", "2.2", "2.3"]),
    ("Кто в дирекции ИБ на инициации при полных данных назначает руководителя проекта и за сколько?",
     "1 рабочего дня", ["2.7", "2.2", "2.3"]),
    ("Кто в дирекции информационной безопасности при инициации проекта при полной информации "
     "назначает РП и в какой срок?", "1 рабочего дня", ["2.7", "2.2", "2.3"]),
    (DB_QUESTION, "вероятности сделки более 80%", ["7.2", "7.1.1", "7.1.2"]),
    (PRE_QUESTION, "отправляет его аккаунт-менеджеру (АМ)", ["1.3"]),
    (TECH_QUESTION, "запрещено использовать для подачи в конкурс", ["2.4", "1.5"]),
])
def test_local_answer_cites_audited_original_without_model(engine, fragments, monkeypatch, text, expected, anchors):
    assembly, _, originals, _ = engine
    allow_originals(originals, fragments)

    def no_model(*args):
        pytest.fail("A verified standalone question should not call the model")

    monkeypatch.setattr(assembly, "_answer", no_model)
    result = assembly.answer(question(text, fragments))
    assert result.action == "answer" and isinstance(result, VerifiedAnswer)
    assert result.resolved_question == text and expected in result.text
    allowed = {source.fragment_id: source for source in fragments}
    assert [allowed[citation.fragment_id].anchor for citation in result.citations] == anchors
    assert all(citation.quote in allowed[citation.fragment_id].text for citation in result.citations)


@pytest.mark.parametrize("text", [
    "В какой срок назначают руководителя проекта при инициации?",
    "В какой срок в Дирекции ИБ назначают руководителя проекта при реализации?",
    "В какой срок в Дирекции ИБ назначают РП при инициации, если нет расчёта?",
    "В какой срок в Дирекции ИБ назначают РП при инициации за координатора в отпуске?",
    "Кто назначает TPS Технической дирекции на проекте ИБ?",
    "А в Дирекции ИБ?",
    IB_QUESTION + " Кто замещает координатора?",
    PRE_QUESTION.replace("от всех услуг", "от внедрения"),
    PRE_QUESTION.replace("явно отказался", "не отказался"),
    PRE_QUESTION.replace("выбрал", "ищет"),
    TECH_QUESTION.replace("для торгов", "для заключения договора"),
])
def test_uncertain_or_different_intent_uses_normal_engine(engine, fragments, monkeypatch, text):
    assembly, checks, originals, _ = engine
    allow_originals(originals, fragments)
    fallback = AnswerResult("clarify", "Уточните ваш вопрос.")
    monkeypatch.setattr(assembly, "_answer", lambda req, calls: fallback)
    assert assembly.answer(question(text, fragments)) is fallback
    assert checks == []


@pytest.mark.parametrize("context", [
    {"history": (HistoryMessage("user", "Условия предыдущего проекта"),)},
    {"conditions": {"project_role": "РП"}},
    {"clarification": {"value": "Дирекция ИБ"}},
    {"clarification_count": 1},
    {"deadline": datetime.now(UTC) - timedelta(seconds=1)},
])
def test_context_and_expired_deadline_disqualify_cached_reply(engine, fragments, context):
    assembly, checks, originals, _ = engine
    allow_originals(originals, fragments)
    assert assembly.fast_answers.answer(question(IB_QUESTION, fragments))
    assert assembly.fast_answers.answer(question(IB_QUESTION, fragments, **context)) is None
    assert len(checks) == 1


def test_cached_reply_rechecks_authorized_originals_and_faq(engine, fragments):
    assembly, checks, originals, faq_rows = engine
    allow_originals(originals, fragments)
    assert assembly.fast_answers.answer(question(IB_QUESTION, fragments))
    assert assembly.fast_answers.answer(question(IB_QUESTION, fragments))
    assert len(checks) == 1
    assert assembly.fast_answers.answer(question(IB_QUESTION, fragments[1:])) is None

    fragment = next(f for f in fragments if f.title == TITLES["IB"] and f.anchor == "2.7")
    for edited in (replace(fragment, text=fragment.text + " Только в рабочее время."),
                   replace(fragment, version=2), replace(fragment, fragment_id=uuid4())):
        changed = tuple(edited if f is fragment else f for f in fragments)
        assert assembly.fast_answers.answer(question(IB_QUESTION, changed)) is None

    new_revision = uuid4()
    changed = tuple(replace(f, revision_id=new_revision, fragment_id=uuid5(new_revision, f.anchor))
                    if f.title == TITLES["IB"] else f for f in fragments)
    assert assembly.fast_answers.answer(question(IB_QUESTION, changed)) is None

    faq = SourceFragment(uuid4(), uuid4(), uuid4(), "Официальное разъяснение.", kind="faq", version=1)
    faq_rows[faq.fragment_id] = dict(current_fragment_id=faq.fragment_id, question_key=TECH_QUESTION, conditions_key="{}")
    with_faq = (*fragments, faq)
    assert assembly.fast_answers.answer(question(IB_QUESTION, with_faq))
    assert assembly.fast_answers.answer(question(IB_QUESTION, with_faq))
    assert len(checks) == 3
    faq_rows[faq.fragment_id]["question_key"] = IB_QUESTION + " Код тестового проекта 68042425."
    assert assembly.fast_answers.answer(question(IB_QUESTION, with_faq))
    faq_rows[faq.fragment_id]["question_key"] = IB_QUESTION
    assert assembly.fast_answers.answer(question(IB_QUESTION, with_faq)) is None
    del faq_rows[faq.fragment_id]
    assert assembly.fast_answers.answer(question(IB_QUESTION, with_faq)) is None


def test_bounded_cache_evicts_least_recent_and_does_not_retain_question(engine, fragments, monkeypatch):
    assembly, checks, originals, _ = engine
    allow_originals(originals, fragments)
    monkeypatch.setattr(fast_answers, "CACHE_SIZE", 2)
    for text in (IB_QUESTION, DB_QUESTION, IB_QUESTION, TECH_QUESTION, IB_QUESTION, DB_QUESTION):
        assert assembly.fast_answers.answer(question(text, fragments))
    assert len(checks) == 4
    assert len(assembly.fast_answers._cache) == 2
    assert all(result.resolved_question is None for result in assembly.fast_answers._cache.values())


def test_missing_curated_claim_never_uses_a_synthesized_claim(engine, fragments):
    assembly, checks, originals, _ = engine
    allow_originals(originals, fragments)
    records = [dict(record, id="IB:2.7:0") if record["id"] == "IB:2.7:1" else record for record in assembly.records]
    local = VerifiedAnswers("unused", lambda: records)
    assert local.answer(question(IB_QUESTION, fragments)) is None
    assert local.answer(question(IB_QUESTION, fragments)) is None
    assert len(checks) == 2 and not local._cache


def _admit(dsn, service, question):
    with connect(dsn) as conn:
        return service.admit(conn, ACTOR, uuid4(), MessageRequest(text=question, client_message_id=str(uuid4())))


def _publish_faq(dsn, text):
    class Uncovered:
        def answer(self, request):
            return AnswerResult("escalate", "Передать вопрос в Отдел качества?", allow_faq=False)

    service = DialogueService(dsn=dsn, engine=Uncovered(), mail_config=MailConfig())
    turn = _admit(dsn, service, text)
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        offer = conn.execute("SELECT id FROM offers WHERE turn_id = %s", (turn.turn_id,)).fetchone()
        service.confirm(conn, ACTOR, offer["id"], 1)
        lock_source_catalog(conn, write=True)
        request = conn.execute("SELECT * FROM mail_requests WHERE offer_id = %s", (offer["id"],)).fetchone()
        publish_faq(conn, request, "Назначение занимает два рабочих дня.")


@pytest.mark.parametrize("change", ["faq", "acl"])
def test_publication_rechecks_faq_and_source_access(dsn, tmp_path, monkeypatch, change):
    seed(dsn, tmp_path)
    assembly = A1Engine(dsn, search=object())
    service = DialogueService(dsn=dsn, engine=assembly)
    first = _admit(dsn, service, IB_QUESTION)
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        turn = conn.execute("SELECT status, result FROM turns WHERE id = %s", (first.turn_id,)).fetchone()
        assert turn["status"] == "completed" and "1 рабочего дня" in turn["result"]["text"]
        assert len(turn["result"]["sources"]) == 3
        assert conn.execute("SELECT count(*) AS n FROM model_calls").fetchone()["n"] == 0

    original_answer = assembly.answer

    def answer_then_change_catalog(request):
        reply = original_answer(request)
        assert isinstance(reply, VerifiedAnswer)
        if change == "faq":
            _publish_faq(dsn, IB_QUESTION)
        else:
            with connect(dsn) as conn:
                lock_source_catalog(conn, write=True)
                conn.execute("UPDATE sources SET acl_actor_ids = ARRAY['other'] WHERE external_id = 'TECH'")
        return reply

    monkeypatch.setattr(assembly, "answer", answer_then_change_catalog)
    second = _admit(dsn, service, IB_QUESTION)
    with pytest.raises(EvidenceChanged, match="source snapshot changed"):
        service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        turn = conn.execute("SELECT status, result FROM turns WHERE id = %s", (second.turn_id,)).fetchone()
        assert turn == {"status": "processing", "result": None}


@pytest.mark.parametrize("select_option", [False, True])
def test_local_reply_after_confirmed_directorate(dsn, tmp_path, select_option):
    class Clarifies:
        def answer(self, request):
            return AnswerResult("clarify", "Какая дирекция ведёт проект?",
                                options=(ClarificationOption("ib", "Дирекция ИБ", {"directorate": "Дирекция ИБ"}),),
                                clarification_key="directorate")

    seed(dsn, tmp_path)
    service = DialogueService(dsn=dsn, engine=Clarifies())
    original = _admit(dsn, service, PM_QUESTION)
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        clarification = conn.execute("SELECT id, version FROM clarifications WHERE turn_id = %s",
                                     (original.turn_id,)).fetchone()
        chosen = {"option_id": "ib"} if select_option else {"text": "Дирекция ИБ"}
        follow = service.admit(
            conn, ACTOR, original.conversation_id,
            MessageRequest(client_message_id=str(uuid4()), case_id=original.case_id,
                           clarification_id=clarification["id"], clarification_version=clarification["version"],
                           **chosen),
        )
    service.engine = A1Engine(dsn, search=object())
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        turn = service.get_turn(conn, ACTOR, follow.turn_id)
        assert turn["status"] == "completed" and turn["result"]["action"] == "answer"
        assert "инициаци" in turn["result"]["text"].casefold()
        assert "1 рабочего дня" in turn["result"]["text"]
        assert turn["result"]["sources"]


def test_explicit_followup_keeps_subject_and_changes_directorate(dsn, tmp_path):
    seed(dsn, tmp_path)
    service = DialogueService(dsn=dsn, engine=A1Engine(dsn, search=object()))
    first = _admit(dsn, service, TECH_QUESTION)
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        follow = service.admit(
            conn, ACTOR, first.conversation_id,
            MessageRequest(text="А в Дирекции ИБ?", client_message_id=str(uuid4()), case_id=first.case_id),
        )
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        turn = service.get_turn(conn, ACTOR, follow.turn_id)
        assert turn["status"] == "completed" and turn["result"]["action"] == "escalate"
        assert "2 недели" not in turn["result"]["text"]
        assert "дирекции иб" in turn["result"]["text"].casefold()
        target = conn.execute("SELECT payload->>'question' AS question FROM offers WHERE turn_id = %s",
                              (follow.turn_id,)).fetchone()
        assert "бюджетной оценки для торгов" in target["question"].casefold()


def test_two_independent_gaps_do_not_silently_drop_a_quality_question(engine, fragments, monkeypatch):
    assembly, _, originals, _ = engine
    allow_originals(originals, fragments)
    fallback = AnswerResult("escalate", "Требуется проверить оба вопроса.")
    monkeypatch.setattr(assembly, "_answer", lambda req, calls: fallback)
    text = f"{TPS_QUESTION} Какой срок подготовки бюджетной оценки для торгов в Дирекции ИБ?"
    assert assembly.answer(question(text, fragments)) is fallback


def test_local_clarification_keeps_previous_phase_context(engine, fragments, monkeypatch):
    assembly, _, originals, _ = engine
    allow_originals(originals, fragments)
    fallback = AnswerResult("clarify", "Уточните этап.")
    monkeypatch.setattr(assembly, "_answer", lambda req, calls: fallback)
    by_dir = dict(key="directorate", value="Дирекция ИБ", choices=("Дирекция ИБ",))
    history = (
        HistoryMessage("user", "Вопрос о реализации"),
        HistoryMessage("assistant", "Ответ", question="Вопрос о реализации", action="answer"),
        HistoryMessage("user", PM_QUESTION),
        HistoryMessage("assistant", "Какая дирекция?", question=PM_QUESTION, action="clarify"),
    )
    request = question(PM_QUESTION, fragments, history=history, clarification=by_dir,
                       clarification_count=1, conditions={"directorate": "Дирекция ИБ"})
    assert assembly.answer(request) is fallback


def test_local_followup_does_not_skip_a_newer_unanswered_message(engine, fragments, monkeypatch):
    assembly, _, originals, _ = engine
    allow_originals(originals, fragments)
    fallback = AnswerResult("clarify", "Уточните, о каком вопросе речь.")
    monkeypatch.setattr(assembly, "_answer", lambda req, calls: fallback)
    history = (HistoryMessage("user", TECH_QUESTION),
               HistoryMessage("assistant", "Ответ", question=TECH_QUESTION, action="answer"),
               HistoryMessage("user", "А как назначают технического специалиста?"))
    assert assembly.answer(question("А в Дирекции ИБ?", fragments, history=history)) is fallback


def test_conflict_review_reuses_the_search_results_from_the_same_turn(monkeypatch):
    from backend.assembly import engine

    calls = []

    def search(text):
        calls.append(text)
        return [], []

    def core_answer(text, lookup, **kwargs):
        lookup(text)
        return {"action": "escalate", "message": "Проверка", "sources": []}

    monkeypatch.setattr(engine.core, "answer", core_answer)
    service = A1Engine("unused", search=SimpleNamespace(snapshot=lambda fragments: ({}, search, {}, {})))
    text = "Кто распределяет экспертов по смежным проектам?"
    assert service.answer(question(text, ())).action == "escalate"
    assert calls == [text]


@pytest.mark.parametrize("faq_race", [False, True])
def test_local_compound_rechecks_faq_for_each_scope(dsn, tmp_path, monkeypatch, faq_race):
    seed(dsn, tmp_path)
    assembly = A1Engine(dsn, search=object())
    service = DialogueService(dsn=dsn, engine=assembly)
    run = assembly.answer
    if faq_race:
        def publish_before_commit(request):
            answer = run(request)
            assert isinstance(answer, VerifiedAnswer)
            _publish_faq(dsn, TPS_QUESTION)
            return answer

        monkeypatch.setattr(assembly, "answer", publish_before_commit)
    turn = _admit(dsn, service, f"{TPS_QUESTION} {TECH_QUESTION}")
    job = claim(dsn, "interactive")
    if faq_race:
        with pytest.raises(EvidenceChanged, match="source snapshot changed"):
            service.answer_job(job)
        return
    service.answer_job(job)
    with connect(dsn) as conn:
        result = service.get_turn(conn, ACTOR, turn.turn_id)["result"]
        assert [part["action"] for part in result["parts"]] == ["escalate", "answer"]
        assert "TPS" in result["parts"][0]["text"]
        assert "2 недели" in result["parts"][1]["text"]
        assert len(result["sources"]) == sum(len(part["sources"]) for part in result["parts"])
        target = conn.execute("SELECT payload->>'question' AS question FROM offers WHERE turn_id = %s",
                              (turn.turn_id,)).fetchone()
        assert target["question"] == TPS_QUESTION


def test_db_training_edition_and_seed_preserve_only_independent_fast_answer(dsn, tmp_path, monkeypatch):
    from backend.assembly.quality_seed import QUESTION as ESTIMATE_QUESTION, seed_quality
    from backend.documents import list_fragments

    seed(dsn, tmp_path)
    assembly = A1Engine(dsn)
    with connect(dsn) as conn:
        baseline = assembly.fast_answers.answer(question(DB_QUESTION, list_fragments(conn, ACTOR.id)))
    assert isinstance(baseline, VerifiedAnswer)

    seed_quality(dsn, tmp_path)
    with connect(dsn) as conn:
        current = list_fragments(conn, ACTOR.id)
        faq = next(f for f in current if f.kind == "faq")
        db_fragments = {f.fragment_id: f for f in current if f.title == TITLES["DB"]}
        assert len({f.document_id for f in current if f.kind == "document"}) == 4
        assert all(f.version == 2 for f in db_fragments.values())
        assert fast_answers.potential_faq(conn, ESTIMATE_QUESTION, {}, current)
    monkeypatch.setattr(assembly, "_answer", lambda *_: pytest.fail("Independent appointment used model path"))
    answer = assembly.answer(question(DB_QUESTION, current))
    assert isinstance(answer, VerifiedAnswer) and answer.text == baseline.text
    assert [ref.quote for ref in answer.citations] == [ref.quote for ref in baseline.citations]
    assert all(ref.fragment_id in db_fragments for ref in answer.citations)
    assert {ref.revision_id for ref in answer.citations}.isdisjoint(ref.revision_id for ref in baseline.citations)

    # The source guard at publication must make the same decision as the fast engine.
    service = DialogueService(dsn=dsn, engine=assembly)
    admitted = _admit(dsn, service, DB_QUESTION)
    service.answer_job(claim(dsn, "interactive"))
    with connect(dsn) as conn:
        tx = conn.execute("SELECT status,result FROM turns WHERE id=%s", (admitted.turn_id,)).fetchone()
        assert tx["status"] == "completed" and tx["result"]["text"] == baseline.text
    # A changed seed, or an additional applicable FAQ, must invalidate cached decisions.
    mutated = tuple(replace(f, text="Назначение выполняет другой сотрудник") if f == faq else f for f in current)
    assert assembly.fast_answers.answer(question(DB_QUESTION, mutated)) is None
    _publish_faq(dsn, DB_QUESTION)
    with connect(dsn) as conn:
        assert assembly.fast_answers.answer(question(DB_QUESTION, list_fragments(conn, ACTOR.id))) is None
