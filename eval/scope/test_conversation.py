import json
import unittest
from unittest.mock import Mock

import ask
import conversation as c
from quality import confirm_request


def item(end, *, relation="new", kind="fact", anchor=None, query=""):
    return dict(through=end, relation=relation, kind=kind, anchor=anchor, query=query)


class ConversationTests(unittest.TestCase):
    def test_implicit_boundary_and_partition_validation(self):
        incoming = "Кто закрывает смету и где хранят заявки?"
        pieces = c.fragments(incoming)
        self.assertEqual(len(pieces), 2)
        parse = lambda *parts: c.parse_plan(json.dumps({"parts": parts}), pieces, [])
        self.assertEqual(parse(item(1), item(2))[1]["query"], "где хранят заявки?")
        self.assertEqual(len(parse(item(2, kind="procedure"))), 1)
        for bad in ([item(1)], [item(2), item(1)], [item(3)], [item(2, relation="history", anchor=1)]):
            with self.assertRaises(ValueError):
                parse(*bad)

    def test_real_history_visible_and_new_facts_rejected(self):
        prior = [("user", "Кто закрывает смету в ДБРЦТ?"), ("assistant", "Это делает РП.")]
        hist = c.visible_history(prior, "А когда?")
        self.assertEqual(hist[0]["assistant"], prior[1][1])
        for query in ("В ИБ в какой срок закрывают смету?", "ДБРЦТ: смету надо закрыть за 5 дней?"):
            with self.assertRaises(ValueError):
                c.parse_plan(json.dumps({"parts": [item(1, relation="history", anchor=1, query=query)]}),
                             ["А когда?"], hist)

    def test_confirmed_request_uses_only_selected_part_and_citations_are_global(self):
        def part(q, response):
            return {"text": q, "query": q, "result": response}
        def direct(ref):
            return dict(action="answer", message="Шаг [1]", sources=[dict(doc="DB", ref=ref)])
        parts = [part("Первая тема", direct("1.1")), part("Вторая тема", direct("2.1")),
                 part("Лимит вложения", ask.reply("escalate", "gap")), part("Личный вопрос", ask.reply("out_of_scope", "pay"))]
        combined = c.assemble(parts)
        self.assertIn("Вторая тема\nШаг [2]", combined["message"])
        logger = Mock()
        confirm_request(combined, "весь текст", "k", confirmed=True, register=logger)
        logger.assert_called_once_with("k", "Лимит вложения")
        parts.append(part("Срок хранения", ask.reply("escalate", "gap")))
        multiple = c.assemble(parts)
        for chosen in (None, 0, 3):
            self.assertIsNone(confirm_request(multiple, "сырой текст", "k", confirmed=True,
                                              part_index=chosen, register=logger))
        self.assertEqual(logger.call_count, 1)

    def test_other_topic_after_question_does_not_reuse_directorate(self):
        raw = "Как мне повысить себе зарплату?"
        history = [("user", "Проверяем смету Технической дирекции"), ("assistant", "Какой этап?")]
        planned = Mock(return_value={"ok": True, "text": json.dumps({"parts": [item(1)]})})
        result = c.answer(raw, Mock(side_effect=AssertionError), history=history, planner=planned)
        self.assertEqual(result["action"], "out_of_scope")
        self.assertFalse(result["offer_quality_request"])
