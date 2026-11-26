import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import judge_pair


class JudgePairTests(unittest.TestCase):
    def test_exactly_one_recipient_and_no_other_difference_in_model_inputs(self):
        q, sources, full, short = judge_pair.inputs()
        spans = judge_pair.fragments(full, sources)
        a = judge_pair.request_for(q, full, sources, spans)
        b = judge_pair.request_for(q, short, sources, spans)
        self.assertEqual(a["model"], b["model"])
        self.assertEqual(a["questions"], b["questions"])
        left, right = (json.loads(x["state"]) for x in (a, b))
        m = left["draft"]["message"]
        self.assertEqual(m.count("направляет его АМ."), 1)
        left["draft"]["message"] = m.replace("направляет его АМ.", "направляет его.")
        self.assertEqual(left, right)
        self.assertEqual(set(left), {"draft", "employee_question", "sources"})
        self.assertGreater(len(sources), 1)
        self.assertGreater(len(spans), 1)
        original = next(s["text"] for s in sources if (s["doc"], s["ref"]) == ("PRE", "1.3"))
        for doc, ref, line in spans.values():
            self.assertEqual((doc, ref), ("PRE", "1.3"))
            self.assertIn(line, original)

    def test_preflight_is_offline_and_does_not_create_live_results(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(judge_pair, "decide", side_effect=AssertionError):
            out = Path(temp) / "live"
            result = judge_pair.experiment(out, check=True)
            self.assertEqual(result["cases"], 2)
            self.assertIn("отправляет его АМ", result["target"][2])
            self.assertFalse(out.exists())
