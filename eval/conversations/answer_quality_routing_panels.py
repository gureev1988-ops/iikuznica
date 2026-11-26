"""Paired routing panels. Historical replies and synthetic inputs stay distinct."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

from answer_quality_routing import ApplicableFAQ, gap_route, restore_object, simple_reply
from answer_quality_trial import Calls, ERRORS, TECHNICAL, Trial, pin, summarize
from check import ask, evidence, fingerprint, load_bank
import dialogue_state
from policy import decision_body, extended
from quality import ApprovedFAQ
from reasoning_trial import run_turn, save

TARGETS = {
    "A3": ("M01.1", "M02.1", "M03.1", "M04.1", "M05.1", "C01.1"),
    "A4": ("P03.2", "P10.2", "D03.2", "D03.3", "D05.2", "D05.3"),
    "K2": ("D01.1", "D01.2", "D01.3", "D02.2", "D05.3", "M02.1", "M03.1", "M04.1", "C01.1"),
}

TOPICS = (
    ("план-график проекта", "реестр рисков"),
    ("отчёт о поставке", "протокол установочного совещания"),
    ("карточку проекта", "заявку на назначение РП"),
    ("смету проекта", "гарантийное письмо"),
    ("заявку на пресейл", "заявку на согласование договора"),
    ("календарь проекта", "протокол приёмки"),
    ("проект договора", "дополнительное соглашение"),
    ("форму заявки", "лист согласования"),
    ("черновик отчёта", "финальный отчёт"),
    ("график работ", "график поставок"),
    ("заявку на доступ", "карточку подрядчика"),
    ("план коммуникаций", "план реагирования"),
)

COMPOUND_PILOT = (
    ("ДБРЦТ. Кто отвечает за мониторинг технических рисков", "какой у него внутренний номер телефона"),
    ("ИБ. Кто публикует пустой план-график в системе", "скажи ещё предельный размер файла на сайте проекта"),
    ("Техническая дирекция. Кто отправляет заказчику финальное ТКП", "какой у него личный телефон"),
    ("ДБРЦТ. Кто отвечает за организацию управления рисками", "можно ли переслать ему сообщение в мессенджере по внутреннему номеру"),
    ("ИБ. Кто предоставляет команде доступ к сайту проекта", "какой фактический размер доступного хранилища для этого сайта"),
    ("Техническая дирекция. Кто ведёт мониторинг поставок и работ", "назови также его почтовый адрес"),
    ("ДБРЦТ. Если у нас есть только проект протокола ОПЭ, можешь проверить, подписан ли он",
     "и укажи, можно ли на этом основании формировать УПД"),
    ("Техническая дирекция. Согласован ли наш договор с подрядчиком, если данных о проекте нет",
     "и можно ли его в таком случае допустить к работам"),
    ("ИБ. Принято ли по нашему проекту решение о старте", "и проведи по этому условию внешнюю установочную встречу"),
    ("ДБРЦТ. Кто отвечает за мониторинг технических рисков", "как РП еженедельно сообщает о здоровье проекта"),
    ("ИБ. Кто публикует площадку проекта", "Кому запрещено предоставлять доступ к сайту проекта"),
    ("Техническая дирекция. Кто отправляет финальное ТКП заказчику", "в каком документе закрепляют изменение базового плана"),
)


def outcome(reply, trial, start, error=None):
    return {**reply, "trace": trial.calls.trace, "events": trial.events,
            "wall_seconds": time.monotonic() - start, **({"error": error} if error else {})}


def checkpoint(out, rows):
    save(out / "rows.json", rows)
    save(out / "summary.json", {group: summarize([r for r in rows if r["group"] == group], panel=False)
                                for group in sorted({r["group"] for r in rows})})


def turn_pair(variant, lookup, out, identifier, question, past, *, reuse=None, faq_pair=(None, None), scope=None):
    row = {"id": identifier, "question": question, "past": past}
    share = out / "control"
    for (arm, active), faq in zip((("control", "C"), ("candidate", variant)), faq_pair):
        calls = Calls(out / arm, identifier, reuse=reuse if arm == "control" else share)
        trial = Trial(active, calls, lookup)
        start = time.monotonic()
        try:
            result = run_turn(out / arm, identifier, question, past, lookup, "C", diagnose=True, observer=trial,
                              faq=faq, faq_scope=scope)
            row[arm] = outcome(result["response"], trial, start)
            row[arm]["diagnostics"] = result["diagnostics"]
        except ERRORS as exc:
            row[arm] = outcome(TECHNICAL, trial, start, str(exc))
    return row


def snapshot(topic, label, days, *, terms, current=True, status="published", role="employee",
             directorate="DB", scope="projects", operation=None):
    answer = f"Действующий срок ({topic}) составляет {days} рабочих дней."
    approval = dict(verifiedText=answer, scope=scope, mailId="pilot-"+label, actor="Автор пилотной записи",
                    date="2026-11-20")
    criteria = {"дирекцию": directorate}
    if operation:
        criteria["вид операции"] = operation
    source = dict(question=topic, answer=answer, answerKind="faq", status=status, approval=approval,
                  discrepancy="Срок в действующей вики отличается.",
                  sources=[dict(kind="wiki", role="discrepancy", text="7 рабочих дней", id="wiki-"+label)])
    return dict(snapshot=source, applicability=dict(mailbox="quality-pilot", current=current,
                roles=[role], conditions=criteria, terms=terms))


def faq_cases():
    topics = ("смета", "протокол", "отчёт", "акт", "график", "предложение",
              "карточка", "заявка", "эскалация", "изменение", "согласование", "претензия")
    changes = ("paraphrase", "revision", "other_scope", "revoked", "stale", "no_access",
               "unknown", "unknown", "unknown", "conflict", "conflict", "conflict")
    for i, (name, change) in enumerate(zip(topics, changes), 1):
        question = f"Какой срок в операции «{name}»?"
        args = dict(topic=question, terms=[["срок", "сколько"], [name]], label=str(i), days=i+1)
        trusted = {"mailbox": "quality-pilot", "role": "employee", "дирекцию": "DB", "вид операции": "release"}
        valid = snapshot(**args)
        first = (question, [valid], trusted, "faq")
        altered = dict(args)
        context = dict(trusted)
        expected = "wiki"
        if change == "paraphrase":
            question = f"Сколько ждать в операции «{name}»?"
            expected = "faq"
        if change == "revision":
            expected = "faq"
            altered.update(days=i+5, label=str(i)+"-rev")
        if change == "other_scope":
            altered["directorate"] = "TECH"
        if change == "revoked":
            altered["status"] = "revoked"
        if change == "stale":
            altered["current"] = False
        if change == "no_access":
            altered["role"] = "manager"
        if change == "unknown":
            altered["operation"] = "release"
            context.pop("вид операции")
            expected = "clarify"
        records = [snapshot(**altered)]
        if change == "revision":
            records.insert(0, snapshot(**{**args, "current": False}))
        if change == "conflict":
            records.append(snapshot(**{**args, "days": i+5, "label": str(i)+"-conflict"}))
            expected = "escalate"
        yield (f"faq-{i}-before", *first)
        yield (f"faq-{i}-{change}", question, records, context, expected)
    # One released clarification survives two wiki revisions, followed by explicit withdrawal.
    question = "Какой срок обработки заявки на расчёт?"
    record = snapshot(question, "sequence", 2, terms=[["срок"], ["заявки"]])
    account = {"mailbox": "quality-pilot", "role": "employee", "дирекцию": "DB"}
    for stage, wiki in enumerate((2, 2, 7, 2, 7), 1):
        item = json.loads(json.dumps(record))
        item["snapshot"]["sources"][0]["text"] = f"{wiki} рабочих дней"
        item["snapshot"]["discrepancy"] = wiki != 2
        if stage == 5:
            item["snapshot"]["status"] = "revoked"
        yield (f"sequence-{stage}", question, [item], account, "faq" if stage < 5 else "wiki")


def faq_panel(out, lookup):
    raw = list(faq_cases())
    pin(out / "frozen.json", json.loads(json.dumps(raw)))
    rows = []
    for identifier, q, records, context, expected in raw:
        result = {"id": identifier, "group": "synthetic", "expected": expected}
        for arm, faq in (("control", ApprovedFAQ(records)), ("candidate", ApplicableFAQ(records, context))):
            start = time.monotonic()

            def wiki(*args, **kwargs):
                text = records[0]["snapshot"]["sources"][0]["text"]
                return dict(action="answer", message=text, sources=[dict(kind="wiki", id="pilot-wiki", text=text)],
                            offer_quality_request=False)

            # Call the normal dialogue entry point and replace the downstream wiki terminal
            # in this snapshot-only panel. Search/generation are controlled identical inputs.
            original = ask.answer
            ask.answer = wiki
            try:
                actual = dialogue_state.answer(q, None, faq=faq, faq_scope="projects")
            finally:
                ask.answer = original
            shown = actual["sources"][0]["kind"] if actual["sources"] else actual["action"]
            result[arm] = dict(actual, shown=shown, correct=shown == expected, trace=[],
                               wall_seconds=time.monotonic() - start)
        rows.append(result)

    from judge_pair import inputs
    q, _, _, _ = inputs()
    bank = {case["id"]: case for case in load_bank()}
    for identifier, question in (("Q04", q), ("C01.1", bank["C01"]["turns"][0]["question"])):
        source = snapshot("Куда направить ТКП по поставке?", identifier, 2,
                          terms=[["выбрал"], ["услуг", "внедрение", "поддержка"]], directorate="IB")
        pub = source["snapshot"]
        answer = "При отказе от услуг BDM направляет АМ ТКП только на поставку."
        pub.update(answer=answer, discrepancy=None)
        pub["approval"]["verifiedText"] = answer
        pub["approval"]["scope"] = "pilot"
        for directorate in ("DB", "IB"):
            account = {"mailbox": "quality-pilot", "role": "employee", "дирекцию": directorate}
            faqs = ApprovedFAQ([source]), ApplicableFAQ([source], account)
            item = turn_pair("A2", lookup, out / "working", f"{identifier}-{directorate}", question, (),
                             faq_pair=faqs, scope="pilot")
            item.update(group="working", faq_snapshot=source, account=account)
            rows.append(item)
    checkpoint(out, rows)


def topic_panel(out, lookup):
    items = []
    for i, (first, second) in enumerate(TOPICS, 1):
        for j in (0, 1):
            other = second if j == 1 and i <= 8 else first
            first_q = f"Я в ДБРЦТ веду {first}. Как его согласовать?"
            second_q = f"Уточни, где хранить {other} после утверждения."
            past = [{"user": q, "resolved": q, "assistant": "Смотри действующую инструкцию." if j == 0
                     else "По этому поводу есть отдельное правило.", "action": "answer"} for q in (first_q, second_q)]
            items.append(dict(id=f"topic-{i}-{j}", q="А когда его нужно обновить?", past=past,
                              expected="ambiguous" if other != first else "follow"))
    pin(out / "pilot-frozen.json", items)

    def run(item):
        start = time.monotonic()
        calls = Calls(out / "pilot", item["id"])
        trial = Trial("A4", calls, lookup)
        q, past = item["q"], item["past"]
        old = "follow", f'В ДБРЦТ когда обновлять {past[0]["user"].split(" веду ", 1)[1].split(".")[0]}?'
        relation, resolved = restore_object(trial, q, past, *old)
        return {**item, "group": "synthetic", "control": dict(action=old[0], message=old[1], trace=[], wall_seconds=0),
                "candidate": outcome(dict(action=relation, message=resolved), trial, start)}

    with ThreadPoolExecutor(max_workers=3) as workers:
        return list(workers.map(run, items))


def compound_panel(out, lookup):
    items = []
    for i, (q1, q2) in enumerate(COMPOUND_PILOT, 1):
        group = "independent" if i <= 6 else "dependent" if i <= 9 else "resolvable"
        for mark, delimiter in (("separate", "? "), ("joined", ", а также ")):
            items.append({"id": f"compound-{i}-{mark}", "q": q1 + delimiter + q2.rstrip("?") + "?",
                          "group": group, "reference_parts": [q1, q2]})
    pin(out / "pilot-frozen.json", items)
    rows = []
    for item in items:
        row = turn_pair("A3", lookup, out / "pilot", item["id"], item["q"], ())
        rows.append(dict(row, group=item["group"], reference_parts=item["reference_parts"]))
        checkpoint(out / "pilot", rows)
    return rows


def retry_panel(corpus, lookup, out, traces):
    inputs = []
    expected_by_turn = {f'{case["id"]}.{i}': {tuple(ref.split()) for part in t["expect"] for ref, _ in part["steps"]}
                        for case in load_bank() for i, t in enumerate(case["turns"], 1)}
    for row in traces.values():
        for event in row["events"]:
            if event["stage"] != "gate" or event["attempt"] or event["route"] != "retrieve":
                continue
            missing = expected_by_turn.get(row["turn"], set()) - {evidence.reference(s) for s in event["sources"]}
            if missing:
                inputs.append(dict(id=row["turn"], group="observed", **event, needed=sorted(map(list, missing))))

    # These inputs deliberately exercise the retry branch. The production verdict is
    # measured separately; its first result cannot be assumed to be retrieve.
    cases = (
        ("db-risk", "ДБРЦТ. Кто и кому сообщает причины вывода проекта из зелёной зоны?", ("DB", "16.10"), ("DB", "16.9")),
        ("tech-bid", "Техническая дирекция. Кто отправляет заказчику финальное ТКП?", ("TECH", "6.4"), ("TECH", "6.2")),
        ("ib-queue", "На пресейле ИБ в каком порядке первая линия обрабатывает открытые заявки?", ("PRE", "3.1"), ("PRE", "3.3")),
        ("db-phone", "ДБРЦТ. Какой телефон у РП проекта для сообщения о выходе из зелёной зоны?", None, ("DB", "16.10")),
        ("db-deadline", "ДБРЦТ. За сколько дней Служба качества подтверждает назначение архитектора проекта?", None, ("TECH", "10.3")),
    )
    for identifier, q, needed, other in cases:
        _, pool = lookup(q)
        for present in (False, True) if needed else (False,):
            src = [corpus[other], *([corpus[needed]] if present else [])]
            draft = simple_reply("escalate", "Не подтверждено.")
            inputs.append(dict(id=identifier+("-present" if present else "-missing"), group="synthetic",
                               question=q, sources=src, pool=pool, draft=draft,
                               verdict={"answers": {"route": {"choice": "retrieve"}}},
                               needed=[list(needed)] if needed else [], present=present))
    pin(out / "frozen.json", inputs)
    rows = []
    for item in inputs:
        identifier, q, sources, pool, draft, verdict = (
            item[k] for k in ("id", "question", "sources", "pool", "draft", "verdict"))
        row = {k: item[k] for k in ("id", "group", "needed")}
        first = {evidence.reference(s) for s in sources}
        target = set(map(tuple, item["needed"]))
        additions = [s for s in extended(sources, pool, corpus) if evidence.reference(s) not in first]
        five = [*sources, *additions[:5]]
        for arm in ("native", "control", "candidate"):
            calls = Calls(out / arm, identifier)
            trial = Trial("K3", calls, lookup)
            start = time.monotonic()
            try:
                if arm == "candidate":
                    route, _, finding, replacement = gap_route(trial, q, sources, pool, draft, verdict, "retrieve", None, 0, None)
                    selected = replacement if replacement is not None else (five if route == "retrieve" else sources)
                else:
                    selected = five if arm == "control" else [*sources, *additions]
                    finding, route = None, "retrieve"
                review = evidence.generate(q, selected, "Перепроверь черновик по оригиналам. " + (finding or ""),
                    call=lambda _, b, d: calls.generate(b, d)["response"])
                body = decision_body(q, review, selected, route == "retrieve")
                body["questions"] = dict(body["questions"], scope=ask.SCOPE)
                result = evidence.judge(body, call=calls.judge)["response"]
                _, reply = ask.scoped_result(result, review, selected, evidence.procedures.requested_fact(q))
                selected_refs = {evidence.reference(s) for s in selected}
                row[arm] = outcome(dict(reply or ask.reply("escalate", "gap"),
                    recovered=sorted(map(list, (selected_refs - first) & target)), sources_after=selected), trial, start)
            except ERRORS as exc:
                row[arm] = outcome(TECHNICAL, trial, start, str(exc))
        rows.append(row)
    checkpoint(out, rows)


def run_panel(variant, corpus, lookup, out, traces_path):
    traces = {r["turn"]: r for r in json.loads(traces_path.read_text())} if traces_path else {}
    if variant == "A2":
        faq_panel(out, lookup)
        return
    if variant == "K3":
        retry_panel(corpus, lookup, out, traces)
        return
    if variant not in TARGETS:
        raise ValueError("Unknown panel")
    bank = {f'{case["id"]}.{i}': t["question"] for case in load_bank()
            for i, t in enumerate(case["turns"], 1)}
    def prepare(identifier):
        recorded = traces.get(identifier)
        if recorded:
            return {"id": identifier, "question": recorded["question"], "past": recorded["past"]}
        if identifier.endswith(".1"):
            return {"id": identifier, "question": bank[identifier], "past": []}
        raise ValueError(f"A control trace is required for {identifier}")

    inputs = [prepare(identifier) for identifier in TARGETS[variant]]
    if variant == "K2":
        inputs.extend(dict(id=f"K2-pair-{i}.{j}", question=q, past=[]) for i, pair in enumerate((
            ("Кого пригласить на установочную встречу в ДБРЦТ?", "Кого пригласить на внутреннюю установочную встречу в ДБРЦТ?"),
            ("Кто формирует УПД в ДБРЦТ после подписания протокола ОПЭ?",
             "Кто формирует УПД в ДБРЦТ после подписания протокола ОПЭ и какой у него телефон?"),
            ("В Дирекции ИБ кто публикует сайт проекта?", "В Дирекции ИБ кто публикует сайт проекта и каков лимит файла?")
        ), 1) for j, q in enumerate(pair, 1))
    pin(out / "frozen.json", inputs)
    rows = []
    for item in inputs:
        row = turn_pair(variant, lookup, out, item["id"], item["question"], item["past"],
                        reuse=None if not traces_path else traces_path.parent)
        rows.append(dict(row, group="pilot" if item["id"].startswith("K2-pair") else "working"))
        checkpoint(out, rows)
    if variant == "A4":
        rows.extend(topic_panel(out, lookup))
    if variant == "A3":
        rows.extend(compound_panel(out, lookup))
    checkpoint(out, rows)
