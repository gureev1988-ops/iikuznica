import json
import unittest
from unittest.mock import Mock

import ask
import dialogue_state as state
import handoff
from quality import confirm_request

Q = "Заказчик ИБ выбрал ПО и прямо сказал, что внедрение и поддержка не нужны. "
MAIN = Q + "Какое предложение ему готовить и кому передать его внутри компании?"
BOTH = Q + "Какое предложение ему готовить, кому передать его внутри компании и в какой срок передать?"
def lookup(_):
    return [("PRE","1.3")], []


class StateTests(unittest.TestCase):
    def test_unsupported_subrequest_stays_with_quality(self):
        direct = state.answer(MAIN, lookup)
        self.assertIn("отправляет его АМ", direct["message"])
        self.assertIn("Заявку на расчет типа «Поставка» на BDM", direct["message"])
        self.assertFalse(direct["offer_quality_request"])
        combined = state.answer(BOTH, lookup)
        self.assertEqual([p["result"]["action"] for p in combined["requests"]], ["answer","escalate"])
        register = Mock()
        confirm_request(combined, BOTH, "1", confirmed=True, register=register)
        register.assert_called_once_with("1", handoff.TRANSFER_QUESTION)

    def test_uncertified_or_changed_conditions_fall_through(self):
        source = ask.full_corpus()["PRE","1.3"]
        for altered in (MAIN.replace("ИБ", "ДБРЦТ"), MAIN.replace("не нужны", "нужны"),
                        MAIN[:-1] + " и почему?", BOTH.replace("срок передать", "срок рассчитывают стоимость")):
            self.assertIsNone(handoff.requested_fields(altered))
        self.assertIsNone(handoff.verified_parts(MAIN, lookup, {("PRE","1.3"):dict(source,text="другая редакция")}, None,None))
        self.assertIsNone(handoff.verified_parts(MAIN, lambda q:([],[]), ask.full_corpus(),None,None))

    def test_known_clarification_does_not_require_model_or_repeat_field(self):
        past = [dict(user="Кого пригласить на установочную встречу?", assistant="Какая дирекция? Какая встреча?",
                     action="clarify", resolved="Кого пригласить на установочную встречу?")]
        result = state.answer("ДБРЦТ.", Mock(side_effect=AssertionError), past=past, planner=Mock(side_effect=AssertionError))
        self.assertEqual(result["action"],"clarify")
        self.assertNotIn("дирекц",result["message"].lower())
        self.assertIn("встреч",result["message"].lower())

    def test_disallows_directorate_from_the_wrong_topic(self):
        past = [dict(user=d, resolved=d, assistant="ответ", action="answer") for d in ["В ДБРЦТ смета", "В ИБ сайт"]]
        call = Mock(return_value=dict(ok=True, text=json.dumps(dict(relation="follow",turn=1,query="Кто согласует смету ИБ?"))))
        relation, _ = state.restore("А кто её согласует?",past,"model",call)
        self.assertEqual(relation,"ambiguous")

    def test_short_followup_resolves_topic_before_matching_faq(self):
        past = [dict(user="ДБРЦТ: что с договором?", assistant="Проверяем договор.",
                     action="answer", resolved="ДБРЦТ: что с договором?")]
        call = Mock(return_value=dict(ok=True, text=json.dumps(
            dict(relation="follow", turn=1, query="Какой срок согласования договора ДБРЦТ?"))))
        faq = Mock()
        faq.lookup.return_value = dict(action="answer", message="ответ качества", sources=[], offer_quality_request=False)
        result = state.answer("А срок?", Mock(side_effect=AssertionError), past=past, planner=call, faq=faq, faq_scope="DB")
        faq.lookup.assert_called_once_with("Какой срок согласования договора ДБРЦТ?", "DB")
        self.assertEqual(result["message"], "ответ качества")
