"""Frozen diagnostic panels. Mutation labels are used only after each call."""
import json

from answer_quality_trial import frozen_gate_panel
from check import evidence, load_bank
import judge_pair

A5_CASES = (
    ("role", "TECH:6.4:1", "Кто отправляет заказчику финальное ТКП Технической дирекции?", "АМ,", "SS,"),
    ("role", "TECH:9.3:1", "Кто назначает РП реализации в Технической дирекции?", "Руководитель отдела управления проектами", "АМ"),
    ("role", "DB:16.2:1", "Кто в ДБРЦТ отвечает за мониторинг технических рисков?", "Архитектор", "РП"),
    ("role", "IB:7.3.1:1", "В ИБ кто запрашивает инженерные ресурсы у Ресурс-менеджеров?", "РП запрашивает", "АМ запрашивает"),
    ("unit", "PRE:5.1:1", "В пресейле ИБ сколько времени у ПА на принятие назначенной заявки?", "одного рабочего дня", "одного календарного дня"),
    ("unit", "TECH:10.3:1", "За какой срок назначают Архитектора в Технической дирекции?", "один рабочий день", "один календарный день"),
    ("unit", "PRE:5.2.1:1", "В каких единицах считают время реакции на пресейле ИБ?", "рабочих часах", "календарных часах"),
    ("unit", "TECH:9.1:1", "Какой срок назначения РП реализации в Технической дирекции?", "один рабочий день", "один календарный день"),
    ("polarity", "IB:7.9.6:1", "Можно ли в ИБ покрыть бюджетом проекта расходы по другому договору?", "Запрещено", "Разрешено"),
    ("polarity", "TECH:15.4:1", "Можно ли оставить резервы в отдельных задачах базового графика Технической дирекции?", "нельзя оставлять", "можно оставлять"),
    ("polarity", "PRE:5.2.2:1", "Меняют ли в пресейле ИБ приоритет заявки из-за нарушения SLA?", "не меняется", "меняется"),
    ("polarity", "TECH:6.6:2", "Продолжают ли работу над сделкой в Технической дирекции, если она невозможна?", "прекращают", "продолжают"),
    ("condition", "DB:20.4:1", "В ДБРЦТ УПД не принят в договорный срок. При каком условии работы считаются принятыми?", "мотивированного отказа нет", "получен мотивированный отказ"),
    ("condition", "TECH:9.2:1", "Какое исключение даёт привлечь РП реализации без договора в Технической дирекции?", "руководителя Технической дирекции", "аккаунт-менеджера"),
    ("condition", "TECH:10.2.2:1", "При каком условии ведут авторский надзор на внедрении в Технической дирекции?", "при наличии договора с заказчиком на эти работы", "даже без договора с заказчиком на эти работы"),
    ("condition", "TECH:15.5:1", "На каком основании можно менять базовый план в Технической дирекции?", "только на основании юридически значимого документа", "по устному согласованию"),
    ("object", "DB:16.5:1", "На каком этапе в ДБРЦТ проводится первичная идентификация рисков?", "на пресейле", "в фазе завершения проекта"),
    ("object", "TECH:6.1:1", "К какому документу TPS прикладывает финальный расчёт Технической дирекции?", "в Заявку на пресейл", "в Заявку на согласование договора"),
    ("object", "PRE:3.1:1", "С каким статусом заявки берёт в общую очередь первая линия пресейла ИБ?", "«Открыто»", "«Дисквалифицировано»"),
    ("object", "IB:7.2.3:1", "Где РП проекта ИБ может запланировать настройку до отправки оборудования?", "на складе", "на площадке заказчика"),
    ("origin", "PRE:5.2.3:1", "С какого момента отсчитывают подготовку расчёта на пресейле ИБ?", "после подтверждения ПА начала расчетов", "после создания заявки"),
    ("origin", "PRE:3.4:2", "От какого события отсчитывают три рабочих дня на получение исходных данных в открытой заявке пресейла ИБ?", "с создания заявки", "с первого запроса"),
    ("origin", "DB:20.2:1", "От какого события отсчитываются два рабочих дня для формирования УПД в ДБРЦТ?", "после подписания Протокола ОПЭ", "после подписания договора"),
    ("origin", "DB:20.3:1", "От какой даты назад считают напоминания РП о статусе УПД в ДБРЦТ?", "до дедлайна", "до даты подписания протокола ОПЭ"),
)

A5_CALIBRATION = (
    ("role", "TECH:17.6:1", "Кто запрашивает отчёт по поставкам без менеджера по поставкам Технической дирекции?",
     "АМ/Менеджер поддержки продаж", "Архитектор"),
    ("role", "IB:4.5:1", "Кто в Дирекции ИБ вносит контакты заказчика для анкетирования?", "РП", "АМ"),
    ("role", "DB:11.4:1", "Кто готовит письмо-резюме по внутренней установочной встрече в ДБРЦТ?", "РП", "АМ"),
    ("condition", "TECH:16.3:1", "Что делает РП, если субподрядчик не прошёл проверку безопасности в Технической дирекции?",
     "повторно запрашивает ресурсы", "прекращает запрос ресурсов"),
    ("condition", "IB:8.2.1:1", "Когда проводят внешнюю установочную встречу в ИБ?", "после", "до"),
    ("object", "DB:14.5:1", "Чьё согласование нужно в ДБРЦТ для передачи бюджета внешним стейкхолдерам?",
     "согласования РП", "согласования заказчика"),
)

ORDER_PILOT = (
    ("ДБРЦТ. Опиши по шагам действия РП при выходе из зелёной зоны проекта.",
     ("DB:16.11:1", "DB:16.10:1", "DB:16.9:1"), ("DB:16.11:1", "DB:16.10:1"),
     ("Для руководителя проекта", "При риске по срокам", "При риске по бюджету",
      "При нехватке ресурсов", "При риске по качеству", "При зависимости от заказчика")),
    ("Техническая дирекция. Опиши по шагам работу с ТКП после пересчёта и внутреннего согласования.",
     ("TECH:6.1:1", "TECH:6.2:1", "TECH:6.8:1", "TECH:6.4:1", "TECH:6.3:1"),
     ("TECH:6.8:1", "TECH:6.4:1"),
     ("Для сделки с заказчиком", "Для обновлённого предложения", "Для финального предложения",
      "После пересчёта стоимости", "После пересчёта сроков", "После пересчёта состава решения")),
)


def sources_for(ids, corpus):
    return [corpus[ref] for ref in dict.fromkeys(tuple(i.split(":")[:2]) for i in ids)]


def render(ids, sources):
    _, allowed = evidence.options(sources)
    return evidence.render({"action": "answer", "ids": list(ids)}, allowed)


def frame(label, group, question, sources, *, ids=None, draft=None, mutated=False):
    return dict(id=label, group=group, question=question, sources=sources,
                ids=ids, draft=render(ids, sources) if draft is None else draft, mutated=mutated)


def replace_once(message, before, after):
    if message.count(before) != 1:
        raise ValueError(f"Mutation must change exactly one span: {before}")
    return message.replace(before, after)


def swap(ids, first, second):
    result = list(ids)
    a, b = result.index(first), result.index(second)
    result[a], result[b] = result[b], result[a]
    return result


def gate_frames(variant, corpus, trace_path):
    bank = {c["id"]: c for c in load_bank()}
    traces = {r["turn"]: r for r in json.loads(trace_path.read_text())} if trace_path else {}

    if variant == "A5":
        question, sources, full, short = judge_pair.inputs()
        replaced = dict(full, message=replace_once(full["message"], "направляет его АМ.", "направляет его SS."))
        yield frame("Q04-full", "working_mutation", question, sources, draft=full)
        yield frame("Q04-removed", "working_mutation", question, sources, draft=short, mutated=True)
        yield frame("Q04-replaced", "working_mutation", question, sources, draft=replaced, mutated=True)
        yield from fact_frames(corpus, A5_CASES, "pilot_pairs")
        return

    yield from claim_frames(variant, corpus, bank, traces)


def fact_frames(corpus, cases, group):
    for n, (kind, claim, question, old, new) in enumerate(cases, 1):
        src = sources_for([claim], corpus)
        full = render([claim], src)
        changed = dict(full, message=replace_once(full["message"], old, new))
        yield frame(f"{kind}-{n}-full", group, question, src, draft=full)
        yield frame(f"{kind}-{n}-changed", group, question, src, draft=changed, mutated=True)


def claim_frames(variant, corpus, bank, traces):
    def procedure(name, ids):
        cid, pos = name.split(".")
        turn = bank[cid]["turns"][int(pos) - 1]
        return frame(name, "working_mutation", turn["question"], sources_for(ids, corpus), ids=ids)

    p01 = [f"TECH:7.{n}:1" for n in range(1, 8)]
    p02 = [f"DB:8.{n}:1" for n in range(4, 9)]
    for name, full, defective in (
        ("P01.1", p01, swap(p01, p01[3], p01[4])),
        ("P02.1", p02, p02[1:]),
    ):
        original = procedure(name, full)
        captured = next((e for e in traces.get(name, {}).get("events", []) if e["stage"] == "gate"), None)
        if captured:
            possible = set(evidence.options(captured["sources"])[1])
            if set(full) <= possible:
                original.update(sources=captured["sources"], question=captured["question"],
                                draft=render(full, captured["sources"]))
        yield original
        yield dict(original, id=name+"-changed", ids=defective,
                   draft=render(defective, original["sources"]), mutated=True)
    if variant == "A1":
        for name, r in traces.items():
            if name.startswith("P") and name.endswith(".1"):
                for i, event in enumerate(e for e in r["events"] if e["stage"] == "gate" and e["ids"]):
                    yield frame(f"{name}-{i+1}", "working_observed", event["question"], event["sources"],
                                ids=event["ids"], draft=event["draft"])
        for question, full, order, contexts in ORDER_PILOT:
            for n, context in enumerate(contexts, 1):
                q = question + " Контекст: " + context + "."
                correct = list(full)
                if n % 2 == 0:
                    correct = [correct[-1], *correct[:-1]]
                defective = swap(correct, *order) if n % 2 else [i for i in correct if i != order[0]]
                label = f"{full[0]}-{n}"
                src = sources_for(full, corpus)
                yield frame(label+"-full", "pilot_pairs", q, src, ids=correct)
                yield frame(label+"-changed", "pilot_pairs", q, src, ids=defective, mutated=True)
        return

    question, sources, _, _ = judge_pair.inputs()
    ids = list(evidence.SUPPLY_HANDOFF_CLAIMS)
    yield frame("Q04-full", "working_mutation", question, sources, ids=ids)
    yield frame("Q04-removed", "working_mutation", question, sources, ids=ids[:1], mutated=True)
    yield frame("Q04-replaced", "working_mutation", question, sources,
                ids=[ids[0], "PRE:1.3:6"], mutated=True)
    change_ids = ["DB:17.1:1", "DB:17.2:1", "DB:17.4:1", "DB:17.5:1", "DB:17.2:2", "DB:17.6:1"]
    full = procedure("P08.1", change_ids)
    yield full
    smaller = [i for i in change_ids if i != "DB:17.5:1"]
    yield dict(full, id="P08.1-changed", ids=smaller, draft=render(smaller, full["sources"]), mutated=True)

    # Both variants express the same material obligation from independent clauses.
    alternatives = ["DB:16.10:1", "DB:16.14:1"]
    q = "ДБРЦТ. При статусе «под риском» кому РП отправляет причины и план возврата в зелёную зону?"
    orig = sources_for([*alternatives, "DB:16.11:1"], corpus)
    for i, key in enumerate(alternatives, 1):
        yield frame(f"equivalent-{i}", "working_mutation", q, orig, ids=[key])
    yield frame("equivalent-changed", "working_mutation", q, orig, ids=["DB:16.11:1"], mutated=True)

    q = "Техническая дирекция. Субподрядный договор подписали, а заказа покупателя с признаком оплаты ещё нет. Субподрядчик может начинать работы?"
    ids = ["TECH:16.4:1", "TECH:16.3:1"]
    src = sources_for(ids, corpus)
    yield frame("P10.2-reference", "working_mutation", q, src, ids=ids[:1])
    yield frame("P10.2-changed", "working_mutation", q, src, ids=ids[1:], mutated=True)


def panel(variant, corpus, lookup, out, trace_path):
    if variant in ("A1", "A5", "K1"):
        return frozen_gate_panel(variant, list(gate_frames(variant, corpus, trace_path)), out)
    from answer_quality_routing_panels import run_panel
    run_panel(variant, corpus, lookup, out, trace_path)
