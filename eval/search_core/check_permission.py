"""Short check of permission coverage, with an optional three-question live run."""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import Mock
import unittest

import core
import permission
from quality import confirm_request

# Fresh working cases from the approved corpus. No closed-set inputs or keys.
CASES = (
    ("payment-missing-act", "Сопоставь даты реализации услуг заказчику и субподрядчику в проекте ИБ. "
     "Клиент перечислил деньги, но со стороны компании акт ещё не завизировали. "
     "Допустимо ли оплатить услуги подрядчика сейчас и кто отвечает за оплату?",
     {("IB", "28.3"), ("IB", "29.6")}, {("IB", "32.1"), ("IB", "32.2"), ("IB", "32.3")}, False),
    ("payment-ready", "Проект Дирекции информационной безопасности. Акт субподрядчика подписан у нас и с "
     "его стороны, закрывающие документы завизированы и в комплекте, от заказчика деньги уже "
     "получили. Можно ли инициировать оплату субподрядчику и кто вправе это сделать?",
     set(), {("IB", "32.1"), ("IB", "32.2"), ("IB", "32.3")}, True),
    ("subcontract-start", "РП Технической дирекции уже видит признак оплаты в заказе покупателя. "
     "Договор с субподрядчиком ещё не подписали. Можно ли привлекать его к выполнению работ сейчас?",
     set(), {("TECH", "16.4")}, False),
)


def response(*items):
    return dict(action="answer", message="\n\n".join(f"{text} [{i}]" for i, (text, _) in enumerate(items, 1)),
                sources=[dict(doc=doc, ref=ref) for _, (doc, ref) in items], offer_quality_request=False)


class PermissionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = core.ask.full_corpus()
        cls.question = CASES[0][1]
        cls.dates = "Даты документов субподрядчика должны быть не позднее даты реализации услуг заказчику."
        cls.draft = response((cls.dates, ("IB", "28.3")))
        cls.full = response((cls.dates, ("IB", "28.3")),
                            ("Сейчас платить нельзя. Акт подписывают обе стороны.", ("IB", "32.3")),
                            ("Оплату инициирует Менеджер по работе с субподрядчиками.", ("IB", "32.1")))

    def review(self, state, prompt):
        if "платить нельзя" not in state["paragraphs"].get(2, ""):
            return dict(covered=False, keep=[1])
        return dict(covered=True, outcome="blocked", decision=state["paragraphs"][2],
                    basis=[dict(doc="IB", ref="32.3", quote=self.corpus["IB", "32.3"]["text"])])

    def check(self, reply, retry, review=None):
        return permission.check_reply(self.question, reply, self.corpus, review or self.review, retry)

    def test_complete_passes_without_search(self):
        self.assertEqual(self.check(self.full, lambda _: self.fail("Unexpected search")), self.full)

    def test_missing_permission_searches_once_and_reviews_again(self):
        searches = []

        def retry(query):
            searches.append(query)
            return self.full

        self.assertEqual(self.check(self.draft, retry), self.full)
        self.assertEqual(len(searches), 1)
        self.assertIn("Дирекция информационной безопасности", searches[0])
        self.assertNotIn("Сопоставь даты", searches[0])

    def test_missing_permission_preserves_verified_dates_and_offers_targeted_escalation(self):
        result = self.check(self.draft, lambda _: self.draft)
        self.assertIn(self.dates, result["message"])
        self.assertTrue(result["offer_quality_request"])
        self.assertEqual(result["requests"][1]["result"]["action"], "escalate")
        self.assertIn("Допустимо ли оплатить", result["escalation_question"])
        self.assertEqual(result["sources"], self.draft["sources"])

    def test_quality_gets_the_missing_question_with_or_without_verified_parts(self):
        for assessment in (self.review, lambda *_: {"covered": False, "keep": []}):
            with self.subTest(assessment=assessment):
                result = self.check(self.draft, lambda _: self.draft, assessment)
                register = Mock()
                confirm_request(result, self.question, "request-1", confirmed=False, register=register)
                register.assert_not_called()
                confirm_request(result, self.question, "request-1", confirmed=True, register=register)
                register.assert_called_once_with("request-1", result["escalation_question"])
                self.assertNotIn("Сопоставь даты", result["escalation_question"])

    def test_foreign_or_invented_quote_cannot_open_gate(self):
        result = self.check(self.full, lambda _: self.full, lambda state, _: dict(
            covered=True, outcome="blocked", decision=state["paragraphs"][2],
            basis=[dict(doc="IB", ref="32.3", quote="цитата, которой нет")]))
        self.assertEqual(result["action"], "escalate")
        self.assertEqual(result["sources"], [])

    def test_non_permission_question_skips_extra_calls(self):
        for question in ("Как датировать акт?", "Можно ли узнать дату оформления акта?"):
            self.assertEqual(permission.check_reply(question, self.draft, self.corpus,
                             lambda *_: self.fail("Unexpected check"), lambda _: self.fail("Unexpected search")), self.draft)

    def test_gate_runs_through_chosen_entrypoint(self):
        calls = []
        seed = [("IB", "28.3")]
        target = [("IB", "32.3")]

        def search(query):
            calls.append(query)
            return (seed if query == self.question else target), []

        def generate(_, body, stage):
            self.assertEqual(stage, "evidence-core")
            originals = json.loads(body["messages"][-1]["content"])["sources"]
            available = {c["id"] for p in originals for c in p["claims"]}
            ids = ["IB:28.3:1"]
            if "IB:32.3:1" in available:
                self.assertIn("Дай прямой ответ", body["messages"][0]["content"])
                ids += ["IB:32.3:1"]
            return dict(ok=True, text=json.dumps(dict(action="answer", ids=ids)))

        def decide(_):
            return {"response": {"answers": {
                "scope": {"type": "choice", "choice": "project", "confidence": .95},
                "route": {"type": "choice", "choice": "answer", "confidence": .95},
                "missing": {"type": "choice", "choice": "directorate"},
                "grounded": {"type": "noul", "noul": .95}, "complete": {"type": "noul", "noul": .95}
            }}}

        def review(state, _):
            for p in state["paragraphs"].values():
                if "только после подписания акта" in p:
                    return dict(covered=True, outcome="blocked", decision=p,
                                basis=[dict(doc="IB", ref="32.3", quote=self.corpus["IB", "32.3"]["text"])])
            return dict(covered=False, keep=[1])

        result = core.answer(self.question, search, call=generate, decide=decide, review=review)
        self.assertEqual(result["sources"], [dict(doc="IB", ref="28.3"), dict(doc="IB", ref="32.3")])
        self.assertEqual(result["resolved_question"], self.question)
        self.assertEqual(len(calls), 2)
        self.assertIn("Допустимо ли оплатить", calls[-1])


def live(args):
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("Set OPENROUTER_API_KEY before the live run.")
    out = args.out
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    rows = []
    for name, question, facts, permission_refs, allowed in CASES:
        checks = []

        def review(state, instruction):
            result = core.review_permission(state, instruction)
            checks.append(result)
            return result

        result = core.answer(question, lambda q: core.ask.retrieve(q, args.model_dir, args.python), review=review)
        refs = {(s.get("doc"), s.get("ref")) for s in result["sources"]}
        row = dict(case=name, question=question, reply=result, reviews=checks,
                   expected_allowed=allowed, expected_permission_refs=sorted(permission_refs),
                   sources_present=bool(refs & permission_refs) and (not facts or bool(refs & facts)),
                   reported_outcome=checks[-1].get("outcome") if checks else None)
        rows.append(row)
        (out / "replies.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f'{name}: {result["action"]}; {row["reported_outcome"]}; sources present={row["sources_present"]}', flush=True)
    print(f"Check that the decision matches expected_allowed and the requested roles in {out / 'replies.json'}.")
    if not all(r["sources_present"] and r["reply"]["action"] == "answer"
               and r["reported_outcome"] == ("allowed" if r["expected_allowed"] else "blocked")
               and not r["reply"]["offer_quality_request"] for r in rows):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.live:
        if not args.model_dir or not args.out:
            parser.error("--live requires --model-dir and --out")
        live(args)
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(PermissionChecks)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            raise SystemExit(1)


if __name__ == "__main__":
    main()
