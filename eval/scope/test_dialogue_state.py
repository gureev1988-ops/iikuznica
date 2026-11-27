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

    def test_procedure_followup_uses_the_same_topic_across_rewritten_answers(self):
        first = "Дирекция ИБ. Как зарегистрировать площадку проекта, раздать доступы и сохранить контакты заказчика?"
        question = "А сотрудника субподрядчика можно туда пригласить?"
        resolved = "Можно ли пригласить сотрудника субподрядчика на площадку проекта Дирекции ИБ?"
        planner = Mock(return_value=dict(ok=True, text=json.dumps(
            dict(relation="follow", turn=1, query=resolved))))
        for action, previous_answer in (
            ("answer", "Сайт проекта создаётся автоматически."),
            ("answer", "Открой сайт и выдай доступ команде. Субподрядчики на сайт не допускаются."),
            ("escalate", "Передать вопрос в отдел качества?"),
        ):
            past = [dict(user=first, resolved=first, assistant=previous_answer, action=action)]
            self.assertEqual(state.restore(question, past, "model", planner), ("follow", resolved))
        old_input, new_input, after_escalation = [call.args[1] for call in planner.call_args_list]
        self.assertEqual(old_input, new_input)
        self.assertEqual(old_input, after_escalation)
        topic, = json.loads(new_input["messages"][1]["content"])["sources"]
        self.assertEqual((topic["user"], topic["resolved"], topic["assistant"]), (first, first, ""))

    def test_procedure_followup_retains_known_directorate_if_resolver_omits_it(self):
        first = "Техническая дирекция. Опиши путь от заявки на субподрядчика до допуска к работам по шагам."
        past = [dict(user=first, resolved=first, assistant="порядок действий", action="answer")]
        resolved = "Может ли субподрядчик начинать работы после подписания договора, если оплаченного заказа ещё нет?"
        planner = Mock(return_value=dict(ok=True, text=json.dumps(
            dict(relation="follow", turn=1, query=resolved))))
        self.assertEqual(state.restore("Договор подписали. Он может начинать?", past, "model", planner),
                         ("follow", "Техническая дирекция. " + resolved))
        self.assertEqual(state.restore("А в другой дирекции он может начинать?", past, "model", planner),
                         ("follow", resolved))
        self.assertEqual(state.restore("А если это ДБРЦТ?", past, "model", planner),
                         ("follow", "ДБРЦТ. " + resolved))
        planner.return_value["text"] = json.dumps(dict(relation="new", turn=None, query=""))
        self.assertEqual(state.restore("Как оформить УПД?", past, "model", planner),
                         ("new", "Как оформить УПД?"))

    def test_procedure_followup_keeps_the_latest_request(self):
        cases = (
            ("Техническая дирекция. В документации был проект договора. Опиши путь до финального "
             "согласования договора и кто подключается по шагам.",
             "Кто меняет наблюдателя и кого ставит вместо себя?", "проект договора", "Техническая дирекция"),
            ("ДБРЦТ. Клиент просит начать работы заранее. Ни договора, ни гарантийного письма нет. "
             "Опиши по шагам согласования и что делать, если срок сорвётся.",
             "АМ подтвердил дату, она прошла, подписи нет. Кому я пишу и когда?", "начать работы заранее", "ДБРЦТ"),
            ("Дирекция ИБ. Как зарегистрировать площадку проекта, раздать доступы и сохранить контакты заказчика?",
             "А сотрудника субподрядчика можно туда пригласить?", "площадку проекта", "Дирекция ИБ"),
        )
        for first, question, subject, directorate in cases:
            with self.subTest(question=question):
                past = [dict(user=first, resolved=first, assistant="Передать вопрос в отдел качества?",
                             action="escalate")]
                decision = dict(relation="follow", turn=1, query=first, subject=subject)
                planner = Mock(return_value=dict(ok=True, text=json.dumps(decision)))
                self.assertEqual(state.restore(question, past, "model", planner),
                                 ("follow", f"{directorate}. Контекст — {subject}. {question}"))
                decision.pop("subject")
                planner.return_value["text"] = json.dumps(decision)
                self.assertEqual(state.restore(question, past, "model", planner)[0], "ambiguous")

    def test_explicit_reference_to_procedure_reply_keeps_the_reply(self):
        first = "В Технической дирекции опиши по шагам, как оформить заявку для нового подрядчика."
        previous_answer = "1. Подать заявку. 2. Дождаться проверки."
        past = [dict(user=first, resolved=first, assistant=previous_answer, action="answer")]
        planner = Mock(return_value=dict(ok=True, text=json.dumps(
            dict(relation="ambiguous", turn=None, query="О какой проверке идёт речь?"))))
        state.restore("Кто отвечает за шаг 2?", past, "model", planner)
        request = planner.call_args.args[1]
        topic, = json.loads(request["messages"][1]["content"])["sources"]
        self.assertEqual(topic["assistant"], previous_answer)

    def test_retries_short_role_question_mistaken_for_a_new_topic(self):
        first = ("Я аккаунт-менеджер Технической дирекции. В документации был проект договора. "
                 "Как довести договор до согласования, кто подключается и в каком порядке?")
        question = "А кто именно меняет наблюдателя и кого ставит вместо себя?"
        past = [dict(user=first, resolved=first, assistant="Передать вопрос в отдел качества?",
                     action="escalate")]
        new = dict(relation="new", turn=None, query=question)
        follow = dict(relation="follow", turn=1, query=first, subject="проект договора")
        planner = Mock(side_effect=[dict(ok=True, text=json.dumps(d)) for d in (new, follow)])
        self.assertEqual(state.restore(question, past, "model", planner),
                         ("follow", f"Техническая дирекция. Контекст — проект договора. {question}"))
        self.assertEqual(planner.call_count, 2)
        planner = Mock(return_value=dict(ok=True, text=json.dumps(new)))
        self.assertEqual(state.restore(question, past, "model", planner), ("new", question))

        other = "А кто именно подписывает УПД в ДБРЦТ?"
        planner.reset_mock()
        self.assertEqual(state.restore(other, past, "model", planner), ("new", other))
        self.assertEqual(planner.call_count, 1)

    def test_retries_subject_copied_from_current_question(self):
        first = "Дирекция ИБ. Как зарегистрировать площадку проекта, раздать доступы и сохранить контакты заказчика?"
        question = "А сотрудника субподрядчика можно туда пригласить?"
        past = [dict(user=first, resolved=first, assistant="Ответ по регламенту ИБ.", action="answer")]
        wrong = dict(relation="follow", turn=1, query=first, subject="сотрудника субподрядчика")
        fixed = dict(wrong, subject="площадку проекта")
        planner = Mock(side_effect=[dict(ok=True, text=json.dumps(d)) for d in (wrong, fixed)])
        self.assertEqual(state.restore(question, past, "model", planner),
                         ("follow", f"Дирекция ИБ. Контекст — площадку проекта. {question}"))
        self.assertEqual(planner.call_count, 2)
        planner = Mock(side_effect=[dict(ok=True, text=json.dumps(wrong)), dict(ok=False, http=429)])
        self.assertEqual(state.restore(question, past, "model", planner)[0], "ambiguous")

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

    def test_directorate_followup_preserves_only_the_explicit_employee_request(self):
        planner = Mock(side_effect=AssertionError("The prior topic is explicit"))
        for prior_question, question, expected in (
            ("Кто назначает РП и в какой срок в ДБРЦТ?", "А в Дирекции ИБ?",
             "Дирекция ИБ. Кто назначает РП и в какой срок?"),
            ("Кто и в какой срок назначают РП на этапе инициации в ДБРЦТ?", "А в Дирекции ИБ?",
             "Дирекция ИБ. Кто и в какой срок назначают РП на этапе инициации?"),
            ("Какой срок подготовки бюджетной оценки для торгов у Технической дирекции?", "А в Дирекции ИБ?",
             "Дирекция ИБ. Какой срок подготовки бюджетной оценки для торгов?"),
            ("В Дирекции ИБ, кто согласует договор?", "А в Технической дирекции?",
             "Техническая дирекция. Кто согласует договор?"),
            ("У нас ИБ. Кто отвечает за поставку?", "А в ДБРЦТ?",
             "ДБРЦТ. Кто отвечает за поставку?"),
        ):
            with self.subTest(question=question, prior=prior_question):
                past = [dict(user=prior_question, resolved=prior_question, action="answer",
                             assistant="АМ, только на этапе инициации, за 5 дней.")]
                self.assertEqual(state.restore(question, past, "model", planner), ("follow", expected))
        planner.assert_not_called()

    def test_directorate_followup_asks_model_if_anchor_or_request_is_unclear(self):
        user = "Кто назначает РП в ДБРЦТ?"
        known = dict(user=user, resolved=user, assistant="РП назначает директор.", action="answer")
        question = "А в Дирекции ИБ?"
        overlapping = "ДБРЦТ. Кто назначает РП в Технической дирекции?"
        cases = (
            (question, [known, dict(known, user="Кто подписывает договор?", resolved="Кто подписывает договор?")]),
            (question, [dict(known, user="А кто именно?")]),
            (question, [dict(known, user=overlapping, resolved=overlapping)]),
            (question, [dict(known, action="clarify")]),
            (question, [dict(known, user="Кто назначает РП на инициации?", resolved="Кто назначает РП на инициации?")]),
            (question, [dict(known, user="Передай вопрос в Отдел качества", resolved="Передай вопрос в Отдел качества")]),
            ("А в Дирекции ИБ кто его замещает?", [known]),
            ("Кто согласует договор в Дирекции ИБ?", [known]),
        )
        for followup, past in cases:
            with self.subTest(question=followup, past=past):
                planner = Mock(return_value=dict(ok=True, text=json.dumps(dict(relation="new", turn=None, query=""))))
                self.assertEqual(state.restore(followup, past, "model", planner), ("new", followup))
                planner.assert_called_once()
