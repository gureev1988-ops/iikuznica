import json
import unittest
from unittest.mock import Mock

import ask
import evidence
from test_scope import decision

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.corpus = ask.full_corpus()

    def test_direct_text_and_refs_are_rendered_from_the_actual_selection(self):
        sources = [self.corpus[("TECH", ref)] for ref in ("23.1", "23.2")]
        _, allowed = evidence.options(sources)
        ids = list(allowed)
        reply = evidence.render({"action": "answer", "ids": ids, "message": "invented"}, allowed)
        self.assertEqual([evidence.reference(s) for s in reply["sources"]], [("TECH", "23.1"), ("TECH", "23.2")])
        for i, key in enumerate(ids, 1):
            self.assertIn(allowed[key]["statement"] + f" [{i}]", reply["message"])
        self.assertNotIn("invented", reply["message"])

    def test_missing_ids_or_changed_source_cannot_produce_instructions(self):
        orig = self.corpus[("TECH", "10.1")]
        _, allowed = evidence.options([orig])
        for ids in ([], ["other:ref:1"], list(allowed) * 2):
            with self.assertRaises(ValueError):
                evidence.render({"action": "answer", "ids": ids}, allowed)
        with self.assertRaises(ValueError):
            evidence.options([dict(orig, text="Different version.")])

    def test_single_directorate_does_not_add_other_directorates(self):
        first = self.corpus[("DB", "1.1")]
        self.assertEqual(evidence.alternatives([first], [dict(doc="DB", ref="1.2", logit=5)], self.corpus), [first])

    def test_judge_receives_the_same_message_as_employee(self):
        clause = self.corpus[("TECH", "10.1")]
        claim = evidence.REGISTRY()[("TECH", "10.1")][0]
        model = Mock(return_value={"ok": True, "text": json.dumps({"action": "answer", "ids": [claim["id"]]})})
        decider = Mock(return_value={"response": decision()})
        result = ask.answer("Техническая дирекция: как назначить ГИПа на новый проект?",
                            lambda _: ([("TECH", "10.1")], [dict(doc="TECH", ref="10.1", logit=1)]),
                            generate_answer=lambda *a: evidence.generate(*a, call=model),
                            judge=lambda body: evidence.judge(body, call=decider),
                            add_sources=evidence.alternatives, instruction="")
        body = decider.call_args.args[0]
        state = json.loads(body["state"])
        self.assertEqual(state["draft"]["message"], result["message"])
        self.assertEqual(result["sources"], [{"doc": clause["doc"], "ref": clause["ref"]}])
        self.assertEqual(result["action"], "answer")


if __name__ == "__main__":
    unittest.main()
