"""Run from the repository root: python3 -m unittest discover -s eval/registry -p test_answer_recovery.py."""
from collections import deque
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import answers


class FormatRecoveryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)
        self.body = {"model": answers.judge.MODEL, "temperature": 0, "messages": [
            {"role": "system", "content": answers.SYSTEM},
            {"role": "user", "content": "Исходные данные для проверки"},
        ]}
        job = {"body": self.body, "guarded": False, "message": "Отправь форму", "anchors": ["DOC 1"],
               "valid": True, "action": "answer", "expected": "answer", "cited": ["DOC 1"], "served": "Отправь форму"}
        self.jobs = {("Z1", route): job for route in answers.compare.ROUTES}
        self.received = []

    def run_grader(self, responses):
        planned = deque(responses)

        def complete(qid, body, dataset):
            response = planned.popleft()
            self.received.append(body)
            row = {"id": qid, "dataset": dataset, "request_hash": answers.judge.fingerprint(body),
                   "http": 200, "ok": True, "latency_s": 0.5, "usage": {"cost": 0.01}}
            if response == "rate_limited":
                return dict(row, http=429, ok=False, text="", error="Rate limit")
            refs = ["DOC 1"] if response == "valid" else []
            grade = {"behavior": "adequate", "reason": "Обосновано", "omissions": [], "claims": [
                {"span": "Отправь форму", "verdict": "supported", "refs": refs, "reason": "Проверено"},
            ]}
            return dict(row, text=json.dumps(grade, ensure_ascii=False))

        output = io.StringIO()
        argv = ["answers.py", "--run", "--out", str(self.out)]
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"}), patch.object(sys, "argv", argv):
            with patch.object(answers, "prepare", return_value=self.jobs), patch.object(answers.judge, "complete", complete):
                with redirect_stdout(output):
                    code = answers.main()
        self.assertFalse(planned, "Expected more calls to the judge")
        return code, output.getvalue()

    def attempts(self):
        lines = (self.out / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def test_format_retry_resumes_after_rate_limit_and_counts_all_costs(self):
        self.assertEqual(self.run_grader(["empty_refs", "rate_limited"])[0], 2)
        self.assertEqual(self.run_grader(["empty_refs", "valid", "valid"])[0], 0)
        self.assertEqual(self.run_grader([])[0], 0)

        rows = self.attempts()
        self.assertEqual([(r["route"], r["format_retry"]) for r in rows],
                         [("bm25", 0), ("bm25", 1), ("bm25", 1), ("bm25", 2), ("registry", 0)])
        for row, request in zip(rows, self.received):
            self.assertEqual(request["model"], self.body["model"])
            self.assertEqual(request["messages"][1], self.body["messages"][1])
            self.assertEqual(row["request_hash"], answers.judge.fingerprint(request))
            if row["format_retry"]:
                self.assertEqual(request["response_format"], {"type": "json_object"})
                self.assertTrue(request["messages"][0]["content"].startswith(answers.SYSTEM))
            else:
                self.assertEqual(request, self.body)

        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["graded"], 2)
        self.assertEqual(summary["grader_attempts"], 5)
        self.assertEqual(summary["grader_format_retries"], 3)
        self.assertAlmostEqual(summary["grader_usage_including_retries"]["cost"]["known_total"], 0.05)

    def test_exhaustion_keeps_both_answers_ungraded_and_stops_new_charges(self):
        attempts_per_route = 1 + len(answers.FORMAT_RETRIES)
        self.assertEqual(self.run_grader(["empty_refs"] * (2 * attempts_per_route))[0], 2)
        code, output = self.run_grader([])
        self.assertEqual(code, 2)
        for route in answers.compare.ROUTES:
            self.assertIn(f"Z1 {route}: format retries exhausted", output)
        self.assertTrue(all(r["invalid_format"] and not r["ok"] for r in self.attempts()))
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["graded"], 0)
        self.assertEqual(summary["grader_attempts"], 2 * attempts_per_route)


if __name__ == "__main__":
    unittest.main()
