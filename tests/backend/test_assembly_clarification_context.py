from types import SimpleNamespace

from backend.assembly import engine as assembly
from backend.contracts.engine import AnswerResult


def test_unknown_fact_keeps_previous_conditions_for_all_rule_comparisons(monkeypatch):
    search = SimpleNamespace(snapshot=lambda fragments: ({}, lambda query: ([], []), {}, {}))
    engine = assembly.A1Engine("", search=search)
    engine.records = []
    monkeypatch.setattr(assembly, "linked_norms", lambda originals, records: {})
    seen = []

    def primary(question, lookup, **kwargs):
        seen.append(question)
        return {"action": "clarify", "sources": [], "message": "На каком этапе проект?"}

    def compare(question, resolved, *args):
        seen.append(question)
        return AnswerResult("escalate", "Уточним оставшееся условие.", escalation_question=resolved)

    monkeypatch.setattr(assembly.core, "answer", primary)
    monkeypatch.setattr(engine, "_conditional_rules", compare)
    request = SimpleNamespace(question="Кто назначает руководителя проекта?", history=(),
                              sources=SimpleNamespace(fragments=()), clarification_count=2,
                              clarification=dict(key="stage", value="Не знаю", original="Не знаю",
                                                 prompt="На каком этапе?", choices=["Пресейл", "Инициация"]),
                              conditions={"directorate": "Дирекция ИБ", "signed": "Договор не подписан",
                                          "stage": "Не знаю"})
    engine._answer(request, SimpleNamespace(chat=None, decisions=None))
    assert len(seen) == 2
    for question in seen:
        assert "Дирекция ИБ" in question and "Договор не подписан" in question
        assert "Сотрудник не знает: На каком этапе?" in question
