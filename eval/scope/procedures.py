"""Source-scoped decisions for project processes, missing facts and conditional instructions."""
from dataclasses import dataclass
from collections import Counter
import re

DIRECTORATES = (
    (re.compile(r"\b(?:иб|диб)\b|информационн\w*\s+безопасност", re.I), ("IB", "PRE")),
    (re.compile(r"\bдбрцт\b|бизнес[- ]решени|цифров\w*\s+трансформаци", re.I), ("DB",)),
    (re.compile(r"тех(?:ническ\w*)?[- .]*дирекци|дирекци\w*\s+технич", re.I), ("TECH",)),
)

FACETS = {
    "directorate": "какая дирекция ведёт проект — информационной безопасности (ИБ), техническая или бизнес-решений и цифровой трансформации (ДБРЦТ)",
    "stage": "на каком этапе проект — пресейл, инициация или реализация",
    "service": "что нужно рассчитать — поставку, услуги или оба направления",
    "meeting": "встреча внутренняя или с заказчиком",
}
PROCESSES = {
    "appoint_pm": (("directorate", "stage"), {"IB": "2.1 2.7", "DB": "7.2", "TECH": "4.1 9.1 9.2"}),
    "estimate_recipient": (("directorate", "service"), {"PRE": "1.3 2.6", "DB": "2.1", "TECH": "2.1"}),
    "kickoff": (("directorate", "meeting"), {"IB": "8.1.1 8.2.2", "DB": "11.2 12.1", "TECH": "11.3 12.1"}),
    "delivery_owner": (("directorate",), {"IB": "27.1", "DB": "14.1", "TECH": "17.1"}),
}


def normalized(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def full_procedure(question):
    if re.search(r"\b(?:отпуск\w*|зарплат\w*|оклад\w*)\b", question, re.I):
        return False
    explicit = re.search(
        r"по шагам|пошагов|порядок действий|в каком порядке|проведи меня|расскажи маршрут|"
        r"опиши путь|какие согласования", question, re.I)
    coordinated_steps = re.search(
        r"\bкак\s+(?:[А-Я]{2,5}\s+)?\w+(?:ть|ти)\b[^?]*(?:,\s*|\bи\s+)(?:где\s+)?\w+(?:ть|ти)\b",
        question, re.I)
    return len(question.split()) >= 12 and bool(explicit or coordinated_steps)


def operation(text):
    t = normalized(text)
    if re.search(r"замещ|замен|отпуск|отсутств|недоступ|сравни|чем различ|чем отлич|для каждой дирекц", t):
        return None
    if (re.search(r"назнач|выдел", t) and re.search(r"\bрп\b|руководител\w* проект", t)
            and re.search(r"сколько|когда|срок|кто", t)):
        return "appoint_pm"
    if (re.search(r"расчет|посчитать", t) and re.search(r"заявк|запрос", t)
            and re.search(r"кому|куда|адресат", t)):
        return "estimate_recipient"
    if re.search(r"установочн\w* (?:встреч|совещан)", t) and re.search(r"кого|участник|позвать|приглас|кто", t):
        return "kickoff"
    if re.search(r"поставк", t) and re.search(r"отвечает|ответствен|контролир|контроль", t) and re.search(r"кто|у кого|на ком", t):
        return "delivery_owner"
    return None


def directories(text):
    return {doc for pattern, docs in DIRECTORATES if pattern.search(text) for doc in docs}


def known(facet, text):
    if facet == "directorate":
        return len({"IB" if doc == "PRE" else doc for doc in directories(text)}) == 1
    patterns = {"stage": r"пресейл|инициац|реализац", "service": r"поставк|услуг|внедрен|техподдерж",
                "meeting": r"внутренн|с заказчиком|внешн"}
    t = normalized(text)
    found = set(re.findall(patterns[facet], t))
    if facet in ("stage", "meeting") and " или " in t and len(found) > 1:
        return False
    return bool(found)


def pending_facets(process, text):
    facets = PROCESSES[process][0]
    if process == "estimate_recipient" and directories(text) in ({"DB"}, {"TECH"}):
        facets = ("directorate",)
    return [facet for facet in facets if not known(facet, text)]


def clarify(facets):
    parts = [FACETS[facet] for facet in facets]
    message = " ".join(part[0].upper() + part[1:] + "?" for part in parts)
    return {"action": "clarify", "message": message, "sources": [], "offer_quality_request": False}


@dataclass(frozen=True)
class Fact:
    name: str
    label: str
    value: str
    subject: str = ""

    def matches(self, passage):
        text = re.sub(r"^\d+(?:\.\d+)*\.\s*", "", normalized(passage["text"]))
        return re.search(self.value, text) is not None and re.search(self.subject, text) is not None


def requested_fact(text):
    t = normalized(text)
    if re.search(r"размер|объем|лимит|ограничен", t) and re.search(r"файл|вложени", t):
        return Fact("file_size", "допустимого размера загружаемого файла",
                    r"\b\d+(?:[,.]\d+)?\s*(?:[кмгkmg][бb]|мегабайт\w*|гигабайт\w*|килобайт\w*)\b", r"файл|вложени")
    if re.search(r"телефон|позвонить|(?:прямой|контактный) номер", t):
        target = re.search(r"(?:телефон\w*|номер)\s+((?:дежурн\w*\s+)?(?:сотрудник\w*\s+)?(?:первой линии|координатор\w*|менеджер\w*|служб\w* \w+))", t)
        label = "номера телефона" + (" " + target[1] if target else " для этого обращения")
        return Fact("phone", label, r"(?<![\d.])(?:\+?\d[ ()-]*){7,15}(?![\d.])|(?:доб\.|тел\.|телефон)\s*[:№]?\s*\d{2,}",
                    r"звон|телефон|тел\.|доб\.|горяч\w* лини|дежурн|контакт|первой линии")
    if re.search(r"кто|кого|кому|заместител|порядок", t) and re.search(r"замещ|замен|в отпуске|недоступ|на время отсутствия", t):
        role = re.search(r"координатор\w* департамент\w*|пресейл[- ]архитектор\w*|руководител\w* проект\w*", t)
        label = "порядка замещения" + (" для роли «" + role[0] + "»" if role else "")
        subject = next((stem for stem in ("координатор", "архитектор", "руководител", "менеджер") if stem in t), "")
        return Fact("substitute", label, r"замещ|замен|другого|в случае (?:его |ее |их )?отсутствия|на время отпуска",
                    subject if subject != "архитектор" else r"архитектор|\bпа\b")
    return None


def fact_sources(fact, question, corpus):
    docs = directories(question)
    ranked = []
    words = {w[:5] for w in re.findall(r"[а-я]{4,}", normalized(question))}
    for key, source in corpus.items():
        if (not docs or key[0] in docs) and fact.matches(source):
            terms = {w[:5] for w in re.findall(r"[а-я]{4,}", normalized(source["text"]))}
            ranked.append((len(words & terms), key))
    ranked.sort(key=lambda item: -item[0])
    return [corpus[key] for _, key in ranked[:6]]


def gap_message(fact):
    return {"action": "escalate", "sources": [], "offer_quality_request": True,
            "message": f"В доступных регламентах я не нашёл подтверждения {fact.label}. Передать вопрос в отдел качества?"}


def specific_project_gap(fact, question, matches):
    if matches or not directories(question):
        return False
    text = normalized(question)
    if fact.name == "file_size":
        return bool(re.search(r"сайт\w* проект|проектн\w* (?:сайт|портал)|регистрац\w* проект|регистриру\w* проект", text))
    if fact.name == "phone":
        return bool(re.search(r"пресейл|проектн\w* (?:заявк|команд)|заявк\w* на (?:проект|расчет)", text))
    return bool(fact.subject and re.search(r"инициаци|назначени\w* (?:рп|руководител)|пресейл", text))


def include_sources(sources, additions):
    return list({(s["doc"], s["ref"]): s for s in [*sources, *additions]}.values())


def section_neighbors(sources, seeds, corpus):
    topics = Counter((p["doc"], p["section"]) for p in (corpus[tuple(key)] for key in seeds))
    result = {(p["doc"], p["ref"]): p for p in sources}
    for topic, _ in topics.most_common(2):
        additions = [p for k, p in corpus.items() if (p["doc"], p["section"]) == topic
                     and p["text"] != p["section"] and k not in result]
        for p in additions[:8]:
            result[p["doc"], p["ref"]] = p
    return list(result.values())


def process_sources(process, question, corpus):
    allowed = directories(question)
    return [corpus[doc, ref] for doc, refs in PROCESSES[process][1].items()
            if not allowed or doc in allowed for ref in refs.split()]


def general_early_start(question):
    t = normalized(question)
    return (bool(re.search(r"начать|начат\w* работ|старт\w* работ|приступить", t)) and "договор" in t
            and bool(re.search(r"каки\w* услов|при каких|возможн|порядок|можно ли|как ", t)))


def letter_status(question):
    t = normalized(question)
    absent = re.search(r"без (?:подписанного )?гарантийн\w* письм|гарантийн\w* письм\w* (?:пока )?нет", t)
    present = re.search(r"гарантийн\w* письм\w* (?:уже )?подписано|есть подписанн\w* гарантийн\w* письм", t)
    if bool(absent) != bool(present):
        return "absent" if absent else "present"
    return "unknown"


def refused_services(question):
    """Require a refusal of all services or of both implementation and support."""
    t = normalized(question)
    if re.search(r"не (?:отказал|отказыва)|не (?:говорил|сказал|подтвердил)|передумал|однако|\bно\b", t):
        return False
    pair = r"(?:внедрени\w* (?:и|или) (?:тех(?:ническ\w* )?)?поддержк\w*|(?:тех)?поддержк\w* (?:и|или) внедрени\w*)"
    declined = r"не (?:нужн\w*|требу\w*|потребу\w*)"
    if re.search(rf"(?:{pair})\s+{declined}|{declined}\s+(?:{pair})", t):
        return True
    # A refusal of a particular service does not exclude the remaining services.
    return bool(re.search(rf"(?:отказал\w* от (?:всех )?услуг|услуги {declined}|{declined} услуги)(?=\s*[.!?,;:]|$)", t))


def supply_handoff(question):
    """The IB presales branch for an already chosen product and an explicit refusal."""
    t = normalized(question)
    if (directories(t) != {"IB", "PRE"} or not refused_services(t)
            or re.search(r"не (?:выбра|определил)|\b(?:если|допустим)\b|ли отказал", t)):
        return False
    item = r"(?:по\b|программн\w* обеспечени\w*|оборудовани\w*|продукт\w*|решени\w*|лицензи\w*)"
    selected = re.search(rf"\bвыбра(?:л[аи]?|ны?)\s+{item}|\b{item}\s+(?:уже )?выбран\w*", t)
    return bool(selected and re.search(r"\bткп\b|предложени\w*|спецификаци\w*", t)
                and re.search(r"кому|куда|кто (?:готовит|переда)", t))


def late_customer_payment(question):
    """Add the conditional collection rule only for a late receipt from the customer."""
    t = normalized(question)
    if directories(t) != {"TECH"} or re.search(r"аванс|предоплат|не просроч|не задерж|нет просрочки|без просрочки", t):
        return False
    payer = r"(?:клиент|заказчик)\w*"
    late = r"(?:задерж\w*|просроч\w*)"
    payment = r"(?:платеж\w*|оплат\w*)"
    return bool(re.search(rf"\b{payer}\s+{late}\s+{payment}\b|\b{late}\s+{payment}\s+(?:со стороны |от ){payer}", t))


def quoted_share(question):
    text = normalized(question)
    if len(re.findall(r"\d+(?:[.,]\d+)?\s*%", text)) != 1:
        return None
    match = re.search(r"(?:стоимость|доля)\s+(?:типового\s+)?проекта\s+"
                      r"(?:(?:составляет|равна|ровно|равно)\s+)*(\d+(?:[.,]\d+)?)\s*%", text)
    return float(match[1].replace(",", ".")) if match else None


def complete_ids(ids, allowed, question):
    ids = list(ids)
    def attach(required):
        if not all(key in allowed for key in required):
            raise ValueError("The linked source for a required condition was not supplied")
        ids.extend(key for key in required if key not in ids)

    if "DB:1.1.1:1" in ids:
        attach(["DB:1.1:1"])
    if "DB:1.6:1" in ids and not re.search(r"без подрядчик|подрядчик\w* не (?:привлек|привлеч|участву)", normalized(question)):
        attach(["DB:1.7:1"])
    if "TECH:23.1:1" in ids and late_customer_payment(question):
        attach(["TECH:23.2:1"])
    if "TECH:9.1:1" in ids:
        attach(["TECH:9.2:1"])
    if general_early_start(question) and any(key.startswith(("DB:8.4:", "DB:8.5:", "DB:8.6:")) for key in ids):
        letter = letter_status(question)
        group = [f"DB:8.{i}:1" for i in range(4, 9)]
        required = group[:1] if letter == "present" else group[1:] if letter == "absent" else group
        ids = [key for key in ids if key not in group]
        attach(required)
    pre = "PRE:1.3:"
    if any(key.startswith(pre) for key in ids):
        if refused_services(question):
            ids = [key for key in ids if key not in {pre+str(i) for i in (2, 3, 4, 6)}]
            attach([pre+"5"])
        elif any(key in ids for key in (pre+"3", pre+"6")):
            share = quoted_share(question)
            if share is not None:
                ids = [key for key in ids if key not in (pre+"3", pre+"6")]
                attach([pre + ("3" if share <= 30 else "6")])
            attach([pre+"5"])
    return ids


def companion_sources(sources, corpus):
    refs = {(s["doc"], s["ref"]) for s in sources}
    needed = []
    for anchor, companion in [(("DB", "1.1.1"), ("DB", "1.1")), (("DB", "1.6"), ("DB", "1.7")),
                              (("TECH", "9.1"), ("TECH", "9.2"))]:
        if anchor in refs:
            needed.append(corpus[companion])
    if any(("DB", f"8.{i}") in refs for i in (4, 5, 6)):
        needed.extend(corpus["DB", f"8.{i}"] for i in range(4, 9))
    return include_sources(sources, needed)


def augment_sources(sources, process, question, matched, corpus):
    extra = process_sources(process, question, corpus) if process else []
    expanded = companion_sources(include_sources(sources, [*extra, *matched]), corpus)
    if late_customer_payment(question) and any((s["doc"], s["ref"]) == ("TECH", "23.1") for s in expanded):
        return include_sources(expanded, [corpus["TECH", "23.2"]])
    return expanded
