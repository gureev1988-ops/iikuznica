import unittest
from unittest.mock import Mock

from ask import answer, pay_shortcut, scoped_result

RULE = {"doc": "DB", "ref": "1.6", "title": "Проектная деятельность ДБРЦТ"}
DIRECT = {"action": "answer", "message": "Ответ по действующему пункту.",
          "sources": [{"doc": "DB", "ref": "1.6"}]}


def decision(scope="project", route="answer", confidence=.95, route_confidence=.9):
    return {"answers": {
        "scope": {"type": "choice", "choice": scope, "confidence": confidence},
        "route": {"type": "choice", "choice": route, "confidence": route_confidence,
                  "probabilities": {"clarify": .01, "escalate": .1, "out_of_scope": .01, "retrieve": .01}},
        "missing": {"type": "choice", "choice": "directorate"},
        "grounded": {"type": "noul", "noul": .95}, "complete": {"type": "noul", "noul": .95}
    }}


class ScopeTests(unittest.TestCase):
    def test_salary_advice_is_local_without_offer(self):
        search = Mock(side_effect=AssertionError)
        for text in ("А как мне повысить себе зарплату?", "Как поднять свой оклад?"):
            result = answer(text, search)
            self.assertEqual(result["action"], "out_of_scope")
            self.assertFalse(result["offer_quality_request"])
            self.assertIn("🙂", result["message"])
        search.assert_not_called()

    def test_no_salary_keyword_ban(self):
        for text in ("Как учесть зарплату в смете проекта?", "Как повысить оклад?",
                     "Как мне повысить себе зарплату? И кто согласует смету проекта?"):
            self.assertFalse(pay_shortcut(text))

    def test_scope_takes_precedence_over_a_tempting_draft(self):
        for scope in ("casual", "other_service", "unclear"):
            with self.subTest(scope=scope):
                _, result = scoped_result(decision(scope), DIRECT, [RULE])
                self.assertEqual(result["action"], "clarify" if scope == "unclear" else "out_of_scope")
                self.assertEqual(result["sources"], [])
                self.assertFalse(result["offer_quality_request"])
                self.assertEqual("🙂" in result["message"], scope == "casual")

    def test_only_confident_project_gap_can_offer(self):
        for scope, route, confidence, expected in (
            ("project", "out_of_scope", .9, "escalate"), ("project", "escalate", .9, "escalate"),
            ("project", "escalate", .55, "clarify"), ("casual", "escalate", .95, "out_of_scope"),
            ("project", "clarify", .95, "clarify"), ("project", "answer", .95, "answer")
        ):
            with self.subTest(scope=scope, route=route, confidence=confidence):
                _, result = scoped_result(decision(scope, route, confidence), DIRECT, [RULE])
                self.assertEqual(result["action"], expected)
                self.assertEqual(result["offer_quality_request"], expected == "escalate")

    def test_original_citation_and_confidence_gates_remain(self):
        remedy, result = scoped_result(decision(), DIRECT, [])
        self.assertEqual(remedy, "revise")
        self.assertIsNone(result)
        _, result = scoped_result(decision(route_confidence=.4), DIRECT, [RULE])
        self.assertEqual(result["action"], "escalate")

    def test_missing_clause_offers_once_after_retry_without_citations(self):
        model = Mock(return_value=DIRECT)
        judge = Mock(return_value={"response": decision(route="retrieve")})
        result = answer("Кто ведёт протокол завершения пресейла?", lambda _: ([], []),
                        generate_answer=model, judge=judge)
        self.assertTrue(result["offer_quality_request"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(judge.call_count, 2)
        for call in judge.call_args_list:
            self.assertIn("scope", call.args[0]["questions"])
        self.assertEqual(model.call_count, 2)

    def test_followup_is_routed_with_the_original_question(self):
        search = Mock(return_value=([], []))
        judge = Mock(return_value={"response": decision(scope="unclear")})
        result = answer("А как мне повысить себе зарплату?", search, "Как мы учитываем затраты проекта?",
                        generate_answer=Mock(return_value=DIRECT), judge=judge)
        self.assertIn("затраты проекта", search.call_args.args[0])
        self.assertFalse(result["offer_quality_request"])


if __name__ == "__main__":
    unittest.main()
