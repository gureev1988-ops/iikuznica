import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from backend.assembly.references import resolve_reference
from backend.contracts.api import MessageRequest
from backend.contracts.engine import AnswerResult
from backend.db import connect
from backend.dialog import Actor, DialogueService
from backend.documents import DocumentSync, PageSnapshot
from backend.runtime import run_once


def test_reference_uses_only_the_employees_current_editions(dsn):
    name = "Сроки приёмки"
    link = "https://docs.example.test/receiving"
    quote = f"Срок ищите в приложении «{name}»."
    source = PageSnapshot("rules", 1, "Приём оборудования",
                          f'<p>Срок ищите в приложении «<a href="{link}">{name}</a>».</p>')
    target = PageSnapshot("times", 1, name, "<p>На приёмку даётся три рабочих дня.</p>",
                          acl_actor_ids=frozenset({"expert"}))
    docs = DocumentSync(dsn, {})
    docs.import_batch("references", (source, target))

    class Engine:
        def answer(self, request):
            refs = {(f.title, f.anchor): f for f in request.sources.fragments}
            if not any(f.title == source.title for f in refs.values()):
                return AnswerResult("out_of_scope", "Источник недоступен.")
            corpus = {key: {"doc": key[0], "ref": key[1], "text": f.text} for key, f in refs.items()}

            def chat(role, body, stage):
                result = {}
                if stage == "document-reference" and request.question != "Кто принимает груз?":
                    result = dict(doc=source.title, ref="intro", name=name, quote=quote,
                                  missing="Каков срок приёмки?", target=target.title)
                if stage == "referenced-rule":
                    result = dict(ref="intro", quote="три рабочих дня", answer="Три рабочих дня.")
                return {"text": json.dumps(result)}

            origin = refs[source.title, "intro"]
            reply = {"action": "answer", "sources": [{"doc": origin.title, "ref": origin.anchor}]}
            resolved = resolve_reference(request.question, None, corpus, refs, SimpleNamespace(chat=chat), reply)
            return resolved or AnswerResult("out_of_scope", "Справочная отсылка.")

    dialog = DialogueService(dsn=dsn, engine=Engine())
    def ask(actor_id, question="За какой срок нужно принять груз?"):
        actor = Actor(actor_id, frozenset({"employee"}), f"{actor_id}@example.test")
        conversation = uuid4()
        with connect(dsn) as conn:
            turn = dialog.admit(conn, actor, conversation, MessageRequest(text=question, client_message_id="ask"))
        assert run_once(dsn, "interactive", {"answer_turn": dialog.answer_job})
        with connect(dsn) as conn:
            return dialog.get_turn(conn, actor, turn.turn_id)["result"]

    internal = ask("expert")
    assert internal["action"] == "answer" and len(internal["sources"]) == 2
    assert internal["sources"][1]["quote"] == "три рабочих дня"
    external = ask("employee")
    assert external["action"] == "escalate"
    assert [ref["related_url"] for ref in external["sources"]] == [link]
    assert ask("expert", "Кто принимает груз?")["action"] == "out_of_scope"

    docs.import_batch("references", (replace(source, version=2, acl_actor_ids=frozenset({"expert"})), target))
    assert ask("employee")["sources"] == []


def test_reference_wording_and_exact_catalog_identity():
    from backend.assembly.references import REFERENCE, catalog_matches

    for clause in ("Срок определяется Регламентом «П1».",
                   "Срок указан в п. 5 инструкции «Расчёт сроков».",
                   "Порог смотрите в документе «Контроль маржи»."):
        assert REFERENCE.search(clause)
    assert not REFERENCE.search("Согласование занимает три рабочих дня.")
    catalog = {"one": "Положение о проектах", "two": "Инструкция №05 от 12.03",
               "three": "Справочник «Тарифы» для другой дирекции"}
    for name in ("Положение о проектировании", "Инструкция №12 от 05.03", "Положение «Тарифы»"):
        assert catalog_matches(name, catalog) == []
    assert catalog_matches("Инструкцией №05 от 12.03", catalog) == ["two"]


def test_reference_search_ranks_only_when_the_seed_can_change_the_origins():
    from backend.assembly.references import _candidate_origins

    pool = [dict(doc="D", ref=str(i), text="Основной пункт.") for i in range(10)]
    corpus = {(p["doc"], p["ref"]): p for p in pool}
    calls = []

    def lookup(question):
        calls.append(question)
        return [("D", "9")], pool

    lookup.lexical_pool = lambda _: pool
    assert _candidate_origins("q", lookup, corpus, set()) == {}
    pool[0]["text"] = "Срок установлен инструкцией о поставке."
    assert _candidate_origins("q", lookup, corpus, set()) == {("D", "0"): pool[0]}
    assert not calls

    pool[0]["text"], pool[9]["text"] = pool[9]["text"], pool[0]["text"]
    assert _candidate_origins("q", lookup, corpus, set()) == {("D", "9"): pool[9]}
    assert calls == ["q"]
    # A cited origin is guaranteed even outside the lexical top eight.
    assert _candidate_origins("q", lookup, corpus, {("D", "9")}) == {("D", "9"): pool[9]}
    assert calls == ["q"]
