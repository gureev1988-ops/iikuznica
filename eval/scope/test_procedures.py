"""Procedure routing, source completeness and the approved FAQ contract."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock

import ask
import evidence
import procedures as p
from quality import ApprovedFAQ, confirm_request

ROOT = Path(__file__).resolve().parents[2]


class ProcedureTests(unittest.TestCase):
    def test_multiple_facets_and_follow_up(self):
        search = Mock(side_effect=AssertionError("incomplete clarification should not search"))
        first = "Кого пригласить на установочную встречу?"
        response = ask.answer(first, search, procedure_mode=True)
        self.assertEqual(response["action"], "clarify")
        self.assertIn("дирекция", response["message"])
        self.assertIn("с заказчиком", response["message"])
        second = ask.answer("ДБРЦТ", search, history=[("user", first), ("assistant", response["message"])],
                            procedure_mode=True)
        self.assertNotIn("дирекция", second["message"])
        self.assertIn("с заказчиком", second["message"])
        sources = p.process_sources("kickoff", "ДБРЦТ", ask.full_corpus())
        self.assertTrue({("DB", "11.2"), ("DB", "12.1")} <= {evidence.reference(s) for s in sources})

    def test_specific_missing_fact_and_positive_counterexamples(self):
        question = "Какой допустимый размер файла на сайте проекта ДБРЦТ?"
        response = ask.answer(question, Mock(side_effect=AssertionError), procedure_mode=True)
        self.assertEqual(response["action"], "escalate")
        self.assertTrue(response["offer_quality_request"])
        self.assertIn("размера", response["message"])
        fact = p.requested_fact(question)
        self.assertTrue(fact.matches({"text": "Ограничение для вложения — до 25 МБ."}))
        self.assertFalse(fact.matches({"text": "Объём работ по проекту — 25 МБ."}))
        phone = p.requested_fact("Телефон дежурного сотрудника первой линии?")
        self.assertTrue(phone.matches({"text": "Телефон дежурного: +7 (495) 123-45-67."}))
        substitute = "ИБ, кто заменяет недоступного пресейл-архитектора?"
        matches = p.fact_sources(p.requested_fact(substitute), substitute, ask.full_corpus())
        self.assertIn(("IB", "3.1"), {evidence.reference(s) for s in matches})

    def test_companions_for_roles_deadlines_and_early_start(self):
        corpus = ask.full_corpus()
        anchors = [corpus[key] for key in (("DB", "1.1.1"), ("DB", "1.6"), ("TECH", "9.1"), ("DB", "8.4"))]
        _, allowed = evidence.options(p.companion_sources(anchors, corpus))
        for anchor, linked in [("DB:1.1.1:1", "DB:1.1:1"), ("DB:1.6:1", "DB:1.7:1"),
                               ("TECH:9.1:1", "TECH:9.2:1")]:
            with self.subTest(anchor=anchor):
                self.assertIn(linked, p.complete_ids([anchor], allowed, "Какие условия действуют?"))
        general = "При каких условиях можно начать работы до подписания договора?"
        branches = [f"DB:8.{n}:1" for n in range(4, 9)]
        self.assertEqual(p.complete_ids(branches[:1], allowed, general), branches)
        self.assertEqual(p.complete_ids(branches[:1], allowed, general + " Гарантийное письмо уже подписано."),
                         branches[:1])
        self.assertEqual(p.complete_ids(branches[:1], allowed, general + " Гарантийного письма нет."), branches[1:])

    def test_percentage_boundary_and_explicit_refusal(self):
        _, allowed = evidence.options([ask.full_corpus()["PRE", "1.3"]])
        low, refused, high = ("PRE:1.3:" + str(i) for i in (3, 5, 6))
        self.assertEqual(p.complete_ids([high], allowed, "Стоимость типового проекта равна 30% стоимости поставки."),
                         [low, refused])
        self.assertEqual(p.complete_ids([low], allowed, "Доля проекта составляет 30,1% стоимости поставки."),
                         [high, refused])
        self.assertEqual(p.complete_ids([low], allowed, "Заказчик отказался от услуг внедрения."), [refused])


def fixture(script):
    module = "const m = require('./demo/faq-revisions/model.js').createModel();\n"
    return json.loads(subprocess.check_output(["node", "-e", module + script], cwd=ROOT, text=True))


class ThresholdTests(unittest.TestCase):
    def test_replay_on_frozen_decisions(self):
        import thresholds
        rows = thresholds.historical(ask.EVAL)
        self.assertEqual([r["correct_actions"] for r in rows], [35, 35, 35, 34, 31])
        self.assertEqual(rows[-1]["excess_escalations"], 2)


class QualityFlowTests(unittest.TestCase):
    def test_confirmation_is_required_and_only_current_approved_faq_can_answer(self):
        pending, active, withdrawn = fixture("""
            const pending = m.query('receipt');
            m.receiveMail('receipt');
            const active = m.query('receipt');
            m.withdrawFaq('receipt', m.state().faqs.find(r => r.id === 'receipt').withdrawToken);
            console.log(JSON.stringify([pending, active, m.query('receipt')]));
        """)
        scope = active["approval"]["scope"]
        alias = "Как оформить тему обращения о получении оценки?"
        def lookup(snapshot):
            return ApprovedFAQ([dict(snapshot=snapshot, approved_aliases=[alias])])
        self.assertIsNone(lookup(pending).lookup(alias, scope))
        self.assertIsNone(lookup(active).lookup(alias, "иная область"))
        self.assertIsNone(lookup(withdrawn).lookup(alias, scope))
        self.assertIsNone(lookup(dict(active, answer="изменённый текст")).lookup(alias, scope))
        answer = ask.answer(alias, Mock(side_effect=AssertionError), faq=lookup(active), faq_scope=scope)
        self.assertEqual(answer["message"], active["answer"])
        self.assertEqual(answer["sources"][0]["id"], "receipt")
        register = Mock(return_value="request-1")
        gap = ask.reply("escalate", "gap")
        self.assertIsNone(confirm_request(gap, alias, "1", confirmed=False, register=register))
        self.assertIsNone(confirm_request(answer, alias, "1", confirmed=True, register=register))
        register.assert_not_called()
        self.assertEqual(confirm_request(gap, alias, "1", confirmed=True, register=register), "request-1")
        register.assert_called_once_with("1", alias)

    def test_approved_expert_resolves_conflict_with_visible_original(self):
        source = fixture("""
            m.syncConfluence();
            m.receiveMail('deadline');
            m.decideMail('deadline', m.state().mails.find(r => r.id === 'deadline').reviewToken, 'approve');
            console.log(JSON.stringify(m.query('estimate')));
        """)
        result = ApprovedFAQ([{"snapshot": source}]).lookup(source["question"], source["approval"]["scope"])
        self.assertEqual(result["message"], source["approval"]["verifiedText"])
        self.assertEqual(result["sources"][0]["id"], "deadline")
        self.assertTrue(result["discrepancy"])
        self.assertTrue(any(s.get("role") == "discrepancy" for s in result["sources"]))
