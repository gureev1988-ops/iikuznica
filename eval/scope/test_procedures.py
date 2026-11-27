"""Procedure routing, source completeness and the approved FAQ contract."""
import json
from pathlib import Path
import subprocess
import unittest
from tempfile import TemporaryDirectory
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
        self.assertEqual(p.complete_ids([low], allowed, "Заказчик отказался от всех услуг."), [refused])


    def test_supply_draft_keeps_handoff_and_still_goes_through_judge(self):
        from test_scope import decision
        question = "Для ИБ заказчик выбрал оборудование и отказался от всех услуг. Кому передать ТКП?"
        generator = Mock(side_effect=AssertionError("both source claims already determine the draft"))
        judge = Mock(return_value={"response": decision()})
        src = ask.full_corpus()["PRE", "1.3"]
        result = ask.answer(question, lambda _: ([("PRE", "1.3")], []),
                            generate_answer=lambda *args: evidence.generate(*args, call=generator),
                            judge=judge, instruction="", procedure_mode=True)
        self.assertEqual(result["action"], "answer")
        self.assertEqual(result["sources"], [{"doc": "PRE", "ref": "1.3"}])
        self.assertIn("BDM", result["message"])
        self.assertIn("направляет его АМ", result["message"])
        self.assertIn("CRM", result["message"])
        self.assertEqual(json.loads(judge.call_args.args[0]["state"])["draft"]["message"], result["message"])
        generator.assert_not_called()
        # A rejected first draft is reconsidered rather than returned on every repair.
        repair = Mock(return_value={"ok": True, "text": '{"action":"clarify","ids":[]}'})
        self.assertEqual(evidence.generate(question, [src], "Пересмотри", call=repair)["action"], "clarify")
        self.assertEqual(evidence.generate(question, [], "", call=repair)["action"], "clarify")
        self.assertEqual(repair.call_count, 2)

    def test_repeated_draft_gets_a_distinct_review_with_all_sources(self):
        from test_scope import decision
        q = "Какие сроки указаны для предварительной оценки?"
        partial = decision()
        partial["answers"]["complete"]["noul"] = .3
        for second, expected in [(decision(), "answer"), (partial, "escalate")]:
            judge = Mock(side_effect=[{"response": partial}, {"response": second}])
            draft = {"action": "answer", "message": "Ответ с проверяемой ссылкой.",
                     "sources": [{"doc": "DB", "ref": "1.6"}]}
            result = ask.answer(q, lambda _: ([("DB", "1.7"), ("DB", "1.6")], []),
                                generate_answer=Mock(return_value=draft), judge=judge, instruction="",
                                procedure_mode=True)
            self.assertEqual(result["action"], expected)
            a, b = [call.args[0] for call in judge.call_args_list]
            self.assertNotEqual(a["questions"]["complete"], b["questions"]["complete"])
            before, after = [json.loads(body["state"])["sources"] for body in (a, b)]
            self.assertCountEqual(before, after)
            self.assertEqual(after[0]["ref"], "1.6")

    def test_supply_fork_requires_scope_selection_and_a_full_refusal(self):
        stem = "ИБ. Клиент выбрал оборудование. Кому передать предложение? "
        for extra in ("От внедрения отказались, поддержка всё ещё нужна.", "Клиент не отказался от услуг.",
                      "Отказался от услуг внедрения.", "Заказчик не сказал, что услуги не нужны."):
            with self.subTest(extra=extra):
                self.assertFalse(p.supply_handoff(stem + extra))
        for other in (stem.replace("выбрал", "не выбрал"), stem.replace("ИБ", "ДБРЦТ"), stem.replace("ИБ. ", "")):
            self.assertFalse(p.supply_handoff(other + "Услуги не нужны."))

    def test_customer_payment_keeps_the_exact_condition_and_both_originals(self):
        src = [ask.full_corpus()["TECH", ref] for ref in ("23.1", "23.2")]
        generator = Mock(return_value={"ok": True, "text": '{"action":"answer","ids":["TECH:23.1:1"]}'})
        question = "Техническая дирекция. Заказчик просрочил оплату. Кто отвечает за контроль?"
        result = evidence.generate(question, src, "", call=generator)
        self.assertEqual([s["ref"] for s in result["sources"]], ["23.1", "23.2"])
        self.assertIn("Если заказчик просрочил оплату выполненных работ", result["message"])
        self.assertEqual(evidence.generate(question, src[:1], "", call=generator)["action"], "escalate")
        for text in ("Заказчик задержал аванс.", "Клиент не задерживает оплату.",
                     "Мы задержали оплату субподрядчику.", "Ожидаем оплату клиента."):
            response = evidence.generate("Техническая дирекция. " + text, src, "", call=generator)
            self.assertEqual([s["ref"] for s in response["sources"]], ["23.1"])


class ReplayTests(unittest.TestCase):
    def test_only_byte_equivalent_request_content_can_reuse_a_model_call(self):
        from run50 import reuse_call
        from run import cached_call

        validate = lambda r: r["text"]
        body = {"model": "example", "question": "Кто получатель?"}
        with TemporaryDirectory() as temp:
            prior = Path(temp)
            (prior / "calls").mkdir()
            cached_call(prior, "example", "generate", body, lambda: {"response": {"text": "A"}}, validate)
            live = Mock(return_value={"response": {"text": "B"}})
            reused = reuse_call(prior, "example", "generate", body, live, validate)
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["response"]["text"], "A")
            live.assert_not_called()
            changed = reuse_call(prior, "example", "generate", dict(body, question="Кто готовит?"), live, validate)
            self.assertEqual(changed["response"]["text"], "B")
            live.assert_called_once()

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

    def test_conflicting_faq_shows_both_positions_and_offers_confirmed_recheck(self):
        source = fixture("""
            m.syncConfluence();
            m.receiveMail('deadline');
            m.decideMail('deadline', m.state().mails.find(r => r.id === 'deadline').reviewToken, 'approve');
            console.log(JSON.stringify(m.query('estimate')));
        """)
        result = ApprovedFAQ([{"snapshot": source}]).lookup(source["question"], source["approval"]["scope"])
        self.assertEqual(result["action"], "answer")
        self.assertIn(source["approval"]["verifiedText"] + " [1]", result["message"])
        self.assertEqual(result["sources"][0]["id"], "deadline")
        self.assertTrue(result["discrepancy"])
        rule = result["sources"][1]
        self.assertEqual(rule["role"], "discrepancy")
        self.assertIn("7 рабочих дней", rule["text"])
        self.assertIn(rule["text"] + " [2]", result["message"])
        self.assertIn("Отправить письмо в отдел качества", result["message"])
        self.assertTrue(result["offer_quality_request"])

        question = source["question"]
        register = Mock()
        confirm_request(result, question, "recheck", confirmed=False, register=register)
        register.assert_not_called()
        confirm_request(result, question, "recheck", confirmed=True, register=register)
        register.assert_called_once_with("recheck", question)

        register.reset_mock()
        confirm_request(dict(result, resolved_question=question), "А какой срок?", "follow",
                        confirmed=True, register=register)
        register.assert_called_once_with("follow", question)

        register.reset_mock()
        from conversation import assemble
        parts = [{"text": question, "query": question, "result": result},
                 {"text": "Другой вопрос", "query": "Другой вопрос", "result": ask.reply("escalate", "gap")}]
        confirm_request(assemble(parts), question, "recheck", confirmed=True, part_index=0, register=register)
        register.assert_called_once_with("recheck:0", question)

    def test_conflict_follows_current_regulation_and_excludes_withdrawn_faq(self):
        question = "Какой срок точной оценки?"
        expert = "2 рабочих дня"
        scope = "ДБРЦТ"
        for revision, days, status in (("v1", 2, "published"), ("v2", 2, "archived"),
                                       ("v3", 7, "published"), ("v3", 7, "withdrawn")):
            with self.subTest(revision=revision, status=status):
                rule = dict(docId="DB", ref="1.6", revision=revision, role="discrepancy", text=f"{days} рабочих дней")
                publication = dict(question=question, answer=expert, answerKind="faq", status=status,
                                   approval=dict(scope=scope, verifiedText=expert, mailId="mail-1",
                                                 actor="Отдел качества", date="2026-11-27"),
                                   discrepancy=days != 2, sources=[rule])
                result = ApprovedFAQ([{"snapshot": publication}]).lookup(question, scope)
                if status in ("archived", "withdrawn"):
                    self.assertIsNone(result)
                elif days == 2:
                    self.assertEqual(result["message"], expert)
                    self.assertFalse(result["offer_quality_request"])
                else:
                    self.assertIn("7 рабочих дней [2]", result["message"])
                    self.assertIn(expert + " [1]", result["message"])
                    self.assertTrue(result["offer_quality_request"])
                    hidden_rule = dict(rule, accessible=False)
                    hidden = dict(publication, sources=[hidden_rule])
                    response = ApprovedFAQ([{"snapshot": hidden}]).lookup(question, scope)
                    self.assertEqual(response["action"], "escalate")
                    self.assertNotIn("7 рабочих дней", response["message"])
