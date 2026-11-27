import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.assembly import quality, quality_seed
from backend.assembly.corpus import seed
from backend.assembly.demo_project import display_example, display_text, renamed_faq
from backend.assembly.engine import A1Engine
from backend.assembly.fast_answers import potential_faq
from backend.contracts.engine import AnswerResult, SourceSnapshot
from backend.db import connect
from backend.documents import list_fragments


OLD_QUESTION = ("Какой точный процент минимальной маржинальности применяется к проекту пресейла ИБ "
                "по внутреннему регламенту контроля маржинальности? Это только для учебного проекта {}.")
OLD_ANSWER = "Только для учебного проекта {} минимальный порог маржинальности составляет 18 процентов"
NAME = "«Тестовый проект iishka»"


@pytest.mark.parametrize("tag", ["QA-e647b18c6d", "JURY-7842"])
def test_display_only_known_training_phrases(tag):
    question = OLD_QUESTION.format(tag)
    answer = OLD_ANSWER.format(tag)
    assert renamed_faq(question, answer)
    assert not renamed_faq(question, OLD_ANSWER.format("JURY-101"))
    assert not renamed_faq(question, answer.replace("18 процентов", "20 процентов"))
    assert display_text(f"{question}\n\n{answer}.").count(NAME) == 2
    assert display_text("Кто руководит проектом JURY-7842?") == "Кто руководит проектом JURY-7842?"
    assert display_text(answer.replace(tag, "JURY-7842-А")) == answer.replace(tag, "JURY-7842-А")
    amended_answer = answer + " при условии предоплаты"
    assert display_text(amended_answer) == amended_answer
    regulation = {"kind": "document", "quote": answer}
    assert display_example([regulation, {"kind": "faq", "quote": answer}]) == [
        regulation, {"kind": "faq", "quote": display_text(answer)}]


def test_existing_scoped_faq_mail_and_citation(dsn, tmp_path, monkeypatch):
    tag = "QA-e647b18c6d"
    question, answer = OLD_QUESTION.format(tag), OLD_ANSWER.format(tag)
    monkeypatch.setattr(quality_seed, "QUESTION", question)
    monkeypatch.setattr(quality_seed, "ANSWER", answer)
    seed(dsn, tmp_path)
    quality_seed.seed_quality(dsn, tmp_path)
    requested = f"Какой минимальный процент маржинальности требуется на пресейле ИБ для проекта {NAME}?"
    with connect(dsn) as conn:
        (row,) = conn.execute(quality.CARD_SELECT, quality.SCOPE).fetchall()
        card = quality.serialize(conn, row)
        assert NAME in card["question"] and NAME in card["answer"]
        assert NAME in card["history"][0]["answer"]
        (thread,) = quality.read_threads(conn, quality.thread_rows(conn))
        assert NAME in thread["preview"]
        assert all(NAME in letter["text"] and tag not in letter["text"] for letter in thread["messages"])
        (fragment,) = list_fragments(conn, "guest", kind="faq")
        assert fragment.text == answer
        assert potential_faq(conn, requested, {}, (fragment,))

    def chat(role, body, purpose):
        (candidate,) = json.loads(body["messages"][-1]["content"])["sources"]
        assert tag not in str(candidate) and NAME in candidate["answer"]
        return {"text": '{"id":1,"scope":"specific","comparison":"gap"}'}

    req = SimpleNamespace(question=requested, case_id=uuid4(), conditions={}, sources=SourceSnapshot((fragment,)))
    result = A1Engine(dsn)._faq(req, SimpleNamespace(chat=chat), AnswerResult("escalate", "Уточнить?"), requested)
    assert result.citations[0].quote == fragment.text
    assert NAME in display_text(result.text) and NAME in display_text(result.citations[0].quote)


@pytest.mark.parametrize("saved_edit", [False, True], ids=["reply-to-open-thread", "save-displayed-card"])
def test_new_name_in_an_old_correspondence(dsn, tmp_path, monkeypatch, saved_edit):
    tag = "JURY-7842"
    question = OLD_QUESTION.format(tag)
    original_answer = OLD_ANSWER.format(tag)
    reply = f"Только для проекта {NAME} на пресейле ИБ минимальный порог маржинальности составляет 18 процентов."
    monkeypatch.setattr(quality_seed, "QUESTION", question)
    monkeypatch.setattr(quality_seed, "ANSWER", original_answer if saved_edit else reply)
    seed(dsn, tmp_path)
    quality_seed.seed_quality(dsn, tmp_path)
    requested = f"Какой минимальный процент маржинальности требуется на пресейле ИБ для проекта {NAME}?"
    with connect(dsn) as conn:
        mail = conn.execute("SELECT * FROM mail_requests").fetchone()
        conn.execute("""INSERT INTO messages(conversation_id,case_id,turn_id,role,text)
            VALUES (%s,%s,%s,'user',%s)""",
            (mail["conversation_id"], mail["case_id"], mail["turn_id"], question))
        if saved_edit:
            (row,) = conn.execute(quality.CARD_SELECT, quality.SCOPE).fetchall()
            shown = quality.serialize(conn, row)

            def local_review(conn, question, answer, conditions):
                return dict(catalog=quality.catalog(conn), result=quality.judge(
                    conn, question, answer, conditions, use_model=False))

            monkeypatch.setattr(quality, "prepare_review", local_review)
            body = quality.Edit(version=row["version"], question=shown["question"], answer=shown["answer"])
            quality.change_card(conn, row["id"], body, "edit", "Эксперт")
        (fragment,) = list_fragments(conn, "guest", kind="faq")
        assert potential_faq(conn, requested, {}, (fragment,))

    def chat(role, body, purpose):
        (candidate,) = json.loads(body["messages"][-1]["content"])["sources"]
        assert tag not in str(candidate) and NAME in candidate["answer"]
        return {"text": '{"id":1,"scope":"specific","comparison":"gap"}'}

    engine = A1Engine(dsn)
    def find(incoming):
        req = SimpleNamespace(question=incoming, case_id=uuid4(), conditions={}, sources=SourceSnapshot((fragment,)))
        return engine._faq(req, SimpleNamespace(chat=chat), AnswerResult("escalate", "Уточнить?"), incoming)

    assert find(requested).citations[0].quote == fragment.text
    assert find(requested.replace(NAME, "JURY-1999")) is None
    assert find(requested.replace(NAME, "iishka-west")) is None
    assert not renamed_faq(display_text(question), reply.replace("18 процентов", "22 процента"), question)
    assert not renamed_faq(display_text(question), reply, question, question.replace(tag, "JURY-1234"))
