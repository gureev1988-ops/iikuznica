import unittest

import ask
import evidence
from check_rendered_plan import check_plan, experiment


class RenderedPlanTests(unittest.TestCase):
    def test_handoff_is_verified_from_the_employee_reply(self):
        report = experiment()
        self.assertIn("отправляет его АМ.", report["source_line"])
        full = report["drafts"]["full"]
        self.assertTrue(full["matches_plan"])
        self.assertIn("направляет его АМ.", full["handoff_proof"]["text"])
        for name in ("missing_recipient", "wrong_recipient"):
            self.assertEqual(report["drafts"][name], {"matches_plan": False, "handoff_proof": None})

    def test_individual_step_and_whole_reply_are_distinct(self):
        sources = [ask.full_corpus()["TECH", ref] for ref in ("23.1", "23.2")]
        plan = ["TECH:23.1:1", "TECH:23.2:1"]
        _, allowed = evidence.options(sources)
        full = evidence.render({"action": "answer", "ids": plan}, allowed)
        self.assertTrue(check_plan(full, plan, sources)["matches_plan"])
        for draft in (dict(full, message=full["message"].replace("оплату выполненных работ", "любой платёж")),
                      dict(full, message=full["message"].replace("[2]", "[1]")),
                      dict(full, message=full["message"].replace("[2]", "[9]"))):
            result = check_plan(draft, plan, sources)
            self.assertFalse(result["matches_plan"])
            self.assertEqual(set(result["proof"]), {plan[0]})
        extra = dict(full, message=full["message"] + "\n\nАМ вправе не сообщать о просрочке. [2]")
        check = check_plan(extra, plan, sources)
        self.assertEqual(set(check["proof"]), set(plan))
        self.assertFalse(check["matches_plan"])
        self.assertFalse(check_plan(full, plan, sources[:1])["matches_plan"])
