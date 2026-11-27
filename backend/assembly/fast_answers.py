"""Audited, source-bound answers for a small set of standalone questions."""
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from threading import Lock
from uuid import uuid5

from backend.contracts.engine import Action, AnswerResult, Citation, ClarificationOption
from backend.db import connect
from .demo_project import asks_about_demo_project, display_text, legacy_faq_tag
from .faq_scope import answer_entities, compatible_ids
from eval.retrieval.extract import TITLES
from eval.scope.procedures import clarify, directories, normalized


# Each pair pins an original DOCX and its complete sequence of extracted clauses
# (sorted by anchor). Any corpus change requires reviewing scope and exceptions again.
ORIGINALS = {
    "DB": (
        "be8a86386276768352e67cfeaef807f98b79f8532bef7c649981326184d4acbd",
        "52e6b43a99cb1274d8480cf4c0aaaca33ddf2e7cde3820fd670dc43120ab4ddc",
    ),
    "IB": (
        "35c9d64940273d19fc27f79e6436056b5944ea975533c53579222c049056b9b4",
        "f963e1a26f6632ea78d23e6a667628c4bdf600d1d70af56d91fcbe0d1f2a1d80",
    ),
    "PRE": (
        "e39235e9a4257c0cb55ccb557198cb9e0f4313d1c4e4e797cdefacc5e279b7f3",
        "a82e73ebcabd043c280c0efac2d7ec831a3c2c68972813335b9251b62e1acc7c",
    ),
    "TECH": (
        "d0a84128c45ba50eccd6715ae534bba0642f1a1b5af1468269d5ad8e079b5dbf",
        "7245b6dc51b3b0de1dd8113537e1e29943cdc48e427baff50835c63b8c22dc2a",
    ),
}
# The training edition changes only DB 1.6. Its exact wording below was
# checked against the approved original and the complete active revision.
DB_TRAINING_REVISION = (
    "b393a87402817a588bc75cc9f29011324977e18db835bdbfb64298cd95769a08",
    "386c756cbd29ca454eeee57c3f6516a0388c92037e4e5cd78010b052f224cf0c",
)
DOC_BY_TITLE = {title: doc for doc, title in TITLES.items()}
POLICY_VERSION = 3
CACHE_SIZE = 64
DIRECTORATE_OPTIONS = ("ДБРЦТ", "Дирекция ИБ", "Техническая дирекция")


@dataclass(frozen=True, slots=True)
class VerifiedAnswer(AnswerResult):
    """A local result whose assumptions depend on the entire visible catalog."""

    checked_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    question: re.Pattern
    text: str
    evidence: tuple[tuple[str, str], ...]
    action: Action = "answer"


# Full matches keep a compound request, exception, or unstated scope out of the fast path.
_APPOINT = r"(?:кто и в какой срок|в какой срок|за сколько времени|когда|кто)"
_ASSIGN = r"(?:назначают|назначает|назначат|должны назначить)"
_PM = r"(?:руководителя проекта|рп)"
_INIT = r"(?:при инициации(?: проекта)?|на этапе инициации(?: проекта)?|на инициации(?: проекта)?)"
_IB_GENITIVE = r"дирекции (?:иб|информационной безопасности)"
_IB = rf"в {_IB_GENITIVE}"
_IB_PREFIX = r"дирекция (?:иб|информационной безопасности)[.,:] "
_DB = r"в дбрцт"
_ESTIMATE_TIME = r"(?:какой срок подготовки бюджетной оценки|сколько времени занимает подготовка бюджетной оценки)"
_TPS = r"(?:tps|технического пресейл[- ]специалиста(?: \(tps\))?)"

_DB_ESTIMATE_TARGET = (
    r"(?:бюджетной(?: оценки)? и точной оценки|точной(?: оценки)? и бюджетной оценки|"
    r"бюджетных и точных оценок|точных и бюджетных оценок)"
)
_DB_ESTIMATE_TERM = r"(?:какие|каковы) (?:рекомендуемые )?сроки(?: рекомендуют)?(?: выдачи)?(?: для)?"
_DB_ESTIMATE_CONTEXT = (
    r"(?:надо|нужно) (?:(?:предупредить|уведомить) заказчика|сообщить заказчику), "
    r"(?:когда ждать оценку|в какие сроки ждать оценку)[.!] "
)
_DB_ESTIMATE = re.compile(
    rf"(?:{_DB} {_DB_ESTIMATE_TERM} {_DB_ESTIMATE_TARGET}|"
    rf"{_DB_ESTIMATE_TERM} {_DB_ESTIMATE_TARGET} {_DB}|"
    rf"{_DB} {_DB_ESTIMATE_CONTEXT}{_DB_ESTIMATE_TERM} "
    rf"(?:{_DB_ESTIMATE_TARGET}|бюджетной и точной|точной и бюджетной))"
)
_DB_EXACT_ESTIMATE = {
    1: "для точных оценок – 5 рабочих дней;",
    2: "для точных оценок – 7 рабочих дней;",
}
_DB_EARLY_PROJECT = r"(?:в дбрцт|на проекте дбрцт)"
_DB_EARLY_WORK = r"(?:начать работы|приступить к работам) до подписания договора"
_DB_EARLY_START = re.compile(
    rf"(?:{_DB_EARLY_PROJECT} (?:(?:заказчик|клиент|команда проекта) (?:хочет|просит) {_DB_EARLY_WORK}[.!] "
    rf"(?:при каких условиях|на каких условиях) это возможно|"
    rf"(?:при каких условиях можно|как|можно ли) {_DB_EARLY_WORK})|"
    rf"(?:при каких условиях можно|как|можно ли) {_DB_EARLY_WORK} {_DB_EARLY_PROJECT})"
)
_IB_PRESALE = r"на пресейле (?:иб|в дирекции (?:иб|информационной безопасности))"
_PACKAGE_TKP = (
    r"можно ли (?:ss|tss) (?:выдать|предоставить|направить) заказчику официальное ткп "
    r"без согласования с (?:па|пресейл[- ]архитектором)"
)
_KICKOFF = r"установочн(?:ую встречу|ое совещание)"
_INVITE = r"кого (?:(?:обязательно|нужно|надо|следует) )?(?:позвать|пригласить)"


_DB_ESTIMATE_RULES = {
    version: Rule(
        _DB_ESTIMATE,
        f"В ДБРЦТ рекомендуют выдавать бюджетную оценку за 3 рабочих дня, точную — за {days} рабочих дней [1]. "
        "Если к оценке подключают подрядчика, дополнительно учитывайте SLA на его поиск и согласование [2].",
        (
            ("DB:1.6:1", _DB_EXACT_ESTIMATE[version] + "\nдля бюджетных оценок – 3 рабочих дня."),
            (
                "DB:1.7:1",
                "В случае подключения к оценке Подрядчика необходимо дополнительно учитывать SLA "
                "на поиск и согласование подрядчика.",
            ),
        ),
    )
    for version, days in ((1, 5), (2, 7))
}


_IB_APPOINTMENT_TEXT = (
    "При полных данных координатор департамента назначает руководителя проекта "
    "в течение 1 рабочего дня в соответствии с итоговым расчётом [1]. "
    "При неполных данных координатор запрашивает уточнения в карточке проекта и ждёт ответа "
    "менеджера по продаже решений (SS) [2]. Если необходимых данных или ответа от SS нет "
    "более 5 рабочих дней, карточку проекта переводят в статус «Отклонён» [3]."
)
_IB_APPOINTMENT_EVIDENCE = (
    (
        "IB:2.7:1",
        "Если предоставлена полная информация, Координатор департамента назначает РП "
        "в соответствии с итоговым Расчётом проекта в течение 1 рабочего дня.",
    ),
    (
        "IB:2.2:1",
        "Если предоставлена неполная информация, Координатор департамента задаёт уточняющие "
        "вопросы в карточке проекта и ожидает ответа от SS (Менеджера по продаже решений).",
    ),
    (
        "IB:2.3:1",
        "В случае отсутствия необходимой информации или ответа от SS более 5 рабочих дней "
        "карточка проекта переводится в статус «Отклонён».",
    ),
)


def _appointment(directorate, *, complete_data=False):
    data = r"(?: при (?:полных данных|полной информации))?" if complete_data else ""
    return re.compile(
        rf"{_APPOINT} (?:{directorate} {_ASSIGN} {_PM} {_INIT}|"
        rf"{_ASSIGN} {_PM} {_INIT} {directorate}|"
        rf"{_ASSIGN} {_PM} {directorate} {_INIT}|"
        rf"{_INIT} {directorate} {_ASSIGN} {_PM}){data}"
    )


_DB_APPOINTMENT = _appointment(_DB)


RULES = (
    Rule(
        re.compile(rf"(?:{_INVITE} на {_KICKOFF}|(?:(?:мне|нам) )?(?:нужно|надо|предстоит) "
                   rf"(?:провести|организовать) {_KICKOFF}[.!] {_INVITE})"),
        clarify(["directorate"])["message"],
        (),
        action="clarify",
    ),
    Rule(
        re.compile(rf"(?:{_appointment(_IB, complete_data=True).pattern}|"
                   rf"{_IB_PREFIX}{_APPOINT} {_ASSIGN} {_PM} {_INIT}|"
                   rf"кто {_IB} {_INIT} при (?:полных данных|полной информации) "
                   rf"{_ASSIGN} {_PM} и (?:за сколько(?: времени)?|в какой срок))"),
        _IB_APPOINTMENT_TEXT,
        _IB_APPOINTMENT_EVIDENCE,
    ),
    Rule(
        re.compile(
            rf"(?:{_IB_PREFIX}{_APPOINT} {_ASSIGN} {_PM}|"
            rf"{_APPOINT} (?:{_IB} {_ASSIGN} {_PM}|{_ASSIGN} {_PM} {_IB}))"
        ),
        f"На этапе инициации проекта в Дирекции ИБ действует следующий порядок. {_IB_APPOINTMENT_TEXT}",
        _IB_APPOINTMENT_EVIDENCE,
    ),
    Rule(
        _DB_APPOINTMENT,
        "На инициации в ДБРЦТ руководитель отдела управления проектами и технологической компетенцией "
        "назначает руководителя проекта не позднее 12 рабочих часов с момента запроса [1]. "
        "Команду формируют при действующем точном расчёте трудозатрат и вероятности сделки более 80% [2]. "
        "Если одно из этих условий не выполнено, срок может измениться [3].",
        (
            (
                "DB:7.2:1",
                "Руководитель отдела управления проектами и технологической компетенцией назначает "
                "на инициацию проекта РП не позднее 12 рабочих часов с момента запроса.",
            ),
            (
                "DB:7.1.1:1",
                "наличие действующего точного расчёта трудозатрат;\nвероятность сделки более 80%.",
            ),
            (
                "DB:7.1.2:1",
                "В случае, если какое-то из условий не выполнено, срок может быть изменён",
            ),
        ),
    ),
    Rule(
        re.compile(
            rf"(?:на пресейле {_IB}|{_IB} на пресейле|для иб) заказчик (?:уже )?выбрал "
            r"(?:по|оборудование|продукт), запросил только расчет поставки и (?:явно )?отказался от всех услуг[.,] "
            r"(?:кто готовит и кому направляет ткп|кому bdm направляет ткп|кому направляют ткп|кому передать ткп)"
        ),
        "BDM готовит ТКП только на поставку и отправляет его аккаунт-менеджеру (АМ) [1].",
        (
            (
                "PRE:1.3:5",
                "Если Заказчик явно обозначил, что ему не потребуются услуги, BDM формирует "
                "ТКП только на поставку и отправляет его АМ.",
            ),
        ),
    ),
    Rule(
        re.compile(
            rf"(?:{_ESTIMATE_TIME} для (?:торгов|конкурса) (?:у|в) технической дирекции|"
            r"можно ли (?:подать бюджетную оценку технической дирекции на конкурс|"
            r"использовать бюджетную оценку технической дирекции для подачи (?:в|на) конкурс))"
        ),
        "Предварительную бюджетную оценку Технической дирекции запрещено использовать для подачи в конкурс [1]. "
        "Для конкурса расчёт нужно инициировать за 2 недели до подачи [2].",
        (
            (
                "TECH:2.4:2",
                "Бюджетная оценка не может использоваться для подачи в конкурс, контрактования "
                "и определения начальной максимальной цены (НМЦ), т.к. данный расчёт не считается окончательным.",
            ),
            ("TECH:1.5:2", "Расчёт должен быть инициирован за 2 недели до подачи конкурса."),
        ),
    ),
    Rule(
        re.compile(
            rf"(?:{_IB_PREFIX}кто назначает {_TPS}|"
            rf"кто (?:{_IB} назначает {_TPS}|назначает {_TPS} {_IB}))"
        ),
        "В доступных регламентах Дирекции ИБ нет правила назначения TPS. По общему порядку при наличии всех "
        "входных данных сотрудник первой линии назначает ответственным за заявку пресейл-архитектора или TSS [1]. "
        "Передать вопрос о назначении TPS в Отдел качества?",
        (
            (
                "PRE:4.1:1",
                "При наличии всех необходимых входных данных Сотрудник 1 линии назначает ответственным "
                "по заявке Пресейл-архитектора или TSS.",
            ),
        ),
        action="escalate",
    ),
    Rule(
        re.compile(
            rf"(?:{_IB_PREFIX}{_ESTIMATE_TIME} для (?:торгов|конкурса|тендера)|"
            rf"{_ESTIMATE_TIME} (?:для (?:торгов|конкурса|тендера) (?:в|у) {_IB_GENITIVE}|"
            rf"{_IB} для (?:торгов|конкурса|тендера)))"
        ),
        "Срок подготовки бюджетной оценки для торгов в доступных регламентах Дирекции ИБ не указан. "
        "По квалифицированному запросу менеджер по продаже решений (SS) по возможности готовит для заказчика "
        "диапазон цен типового проекта [1]. Для тендера SS проверяет комплектность и актуальность документов, "
        "на которых основан расчёт пресейл-архитектора [2]. Если документы изменились или архитектор получил "
        "неполный комплект, SS запрашивает актуализацию расчёта проекта до подачи тендерной заявки [3]. "
        "Передать вопрос о сроке в Отдел качества?",
        (
            (
                "PRE:2.2:2",
                "готовит Бюджетную оценку проекта для Заказчика в виде диапазона цен типового проекта по данной теме "
                "(при наличии такой возможности)",
            ),
            (
                "PRE:16.4:1",
                "Была передана ПА в полном объеме для подготовки расчета проекта,\n"
                "Заказчиком не вносились правки в тендерную документацию, на основании которой ПА делал расчет.",
            ),
            (
                "PRE:16.4.1:1",
                "SS направляет ПА запрос на актуализацию расчета проекта до подачи тендерной заявки.",
            ),
        ),
        action="escalate",
    ),
    Rule(
        re.compile(
            r"кто (?:замещает|заменяет) координатора (?:проектов|департамента) "
            rf"{_IB_GENITIVE} (?:на время отпуска|во время отпуска)"
        ),
        "В доступных регламентах Дирекции ИБ не указан порядок замещения координатора на время отпуска. "
        "Назначение руководителя проекта поручено координатору департамента после уведомления из CRM [1]. "
        "Передать вопрос о замещении в Отдел качества?",
        (
            (
                "IB:2.1:1",
                "Назначение выполняет Координатор департамента после получения уведомления по электронной почте из CRM.",
            ),
        ),
        action="escalate",
    ),
    Rule(
        re.compile(
            rf"(?:{_IB_PRESALE} (?:продаем|продается) пакетное предложение[.!] {_PACKAGE_TKP}|"
            rf"{_PACKAGE_TKP} при продаже пакетного предложения {_IB_PRESALE}|"
            rf"{_IB_PRESALE} {_PACKAGE_TKP} при продаже пакетного предложения)"
        ),
        "Да. При продаже пакетного предложения на пресейле ИБ согласование официального ТКП "
        "с пресейл-архитектором не требуется. Это исключение из общего правила для SS и TSS [1].",
        (
            (
                "PRE:2.3:1",
                "SS и TSS запрещается без согласования с ПА (Пресейл-архитектором) предоставлять заказчику "
                "официальное ТКП, кроме случаев перепродажи вендорского сервиса или продажи пакетных предложений.",
            ),
        ),
    ),
    Rule(
        _DB_EARLY_START,
        "Для начала работ в ДБРЦТ общее правило требует подписанного договора и согласованной сметы [1].\n\n"
        "С гарантийным письмом. Если заказчик подписал письмо, получите письменное согласование директора "
        "направления [1].\n\n"
        "Без гарантийного письма. АМ направляет запрос на ранний старт с плановой датой подписания. "
        "РП запрашивает команду и рассчитывает стоимость работ в рублях до этой даты. АМ получает расчёт для "
        "согласования с владельцем бюджета [2]. После того как владелец бюджета подтвердит возможность "
        "компенсации, решение принимает директор направления. Работы можно начать после его согласования [3].\n\n"
        "За 5 рабочих дней до плановой даты подписания РП запрашивает у АМ подтверждение. Если дату перенесли, "
        "нужно увеличить сумму затрат производства и повторить согласование, в том числе с владельцем бюджета [4]. "
        "Если АМ подтвердил дату, а договор в срок не подписан, РП в тот же день обращается к владельцу бюджета, "
        "АМ, SS и директору направления. Они определяют дальнейшие действия, вплоть до остановки работ "
        "или назначения финальной контрольной точки подписания [5].",
        (
            (
                "DB:8.4:1",
                "Работы со стороны команды проекта без подписанного договора и согласованной сметы запрещены. "
                "Исключение – наличие подписанного гарантийного письма со стороны Заказчика и письменное "
                "согласование Директора направления.",
            ),
            (
                "DB:8.5:1",
                "В случае отсутствия Гарантийного письма запрос на старт работ до подписания договора АМ "
                "направляет с указанием плановой даты подписания договора. Далее РП запрашивает формирование "
                "команды проекта и производит расчёт стоимости работ в рублях до даты подписания. После чего "
                "информация направляется АМ для согласования с Владельцем бюджета.",
            ),
            (
                "DB:8.6:1",
                "После получения подтверждения о возможности компенсации работ со стороны Владельца бюджета "
                "Директор направления принимает решение о согласовании старта работ. В этом случае работы "
                "могут быть начаты только после получения согласования Директора направления.",
            ),
            (
                "DB:8.7:1",
                "За 5 рабочих дней до наступления плановой даты подписания Договора РП запрашивает у АМ "
                "подтверждение того, что договор будет подписан в срок. В случае сдвига даты подписания процесс "
                "согласования старта без договора, описанный выше, повторяется с учётом корректировки суммы "
                "затрат производства в большую сторону и повторного согласования с Владельцем бюджета.",
            ),
            (
                "DB:8.8:1",
                "РП в этот же день инициирует эскалацию на Владельца бюджета, АМ, SS и Директора направления "
                "с запросом дальнейших действий по Заказчику.",
            ),
        ),
    ),
)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def faq_candidate(row, question_scope, normalized_question, conditions):
    known_scope = directories(row["question_key"])
    return (not known_scope or known_scope == question_scope) and all(
        conditions.get(key) == value
        or (isinstance(value, str) and normalized(value) in normalized_question)
        for key, value in json.loads(row["conditions_key"]).items()
    )


def faq_identity_matches(key, answer, question, conditions, original=None):
    if asks_about_demo_project(question, conditions) and legacy_faq_tag(key, answer, original):
        key, answer = display_text(key), display_text(answer)
    return compatible_ids(key + "\n" + answer_entities(answer), question, conditions)


def potential_faq(conn, question, conditions, fragments):
    blocked, _ = _check_faq(conn, question, conditions, fragments)
    return blocked


def _check_faq(conn, question, conditions, fragments):
    """Return unknown applicability and the approved seed when its scope is certain."""
    faq = {fragment.fragment_id: fragment for fragment in fragments if fragment.kind == "faq"}
    if not faq:
        return False, None
    rows = conn.execute("""
        SELECT f.current_fragment_id, f.question_key, f.conditions_key, f.status, r.pending, r.question AS original_question,
               seed.faq_id IS NOT NULL AS is_seed
        FROM faq_entries f JOIN mail_requests r ON r.id=f.request_id
        LEFT JOIN assembly_quality_seed seed ON seed.faq_id=f.id
        WHERE f.current_fragment_id = ANY(%s::uuid[])
    """, (list(faq),)).fetchall()
    if {row["current_fragment_id"] for row in rows} != faq.keys():
        return True, None
    scope, text = directories(question), normalized(question)
    intent = text.removesuffix("?").rstrip()
    applicable_seed = None
    for row in rows:
        fragment = faq[row["current_fragment_id"]]
        if not faq_identity_matches(row["question_key"], fragment.text, question, conditions, row.get("original_question")):
            continue
        if not faq_candidate(row, scope, text, conditions):
            continue
        if not _is_reviewed_seed(row, fragment):
            return True, None
        if _DB_APPOINTMENT.fullmatch(intent) or _DB_EARLY_START.fullmatch(intent):
            # The estimate lead time has no bearing on appointment or early start.
            continue
        if not conditions and _DB_ESTIMATE.fullmatch(intent):
            # The exact-estimate part has the same scope, so the answer must cite
            # the seed separately alongside the current regulation.
            applicable_seed = fragment
            continue
        return True, None
    return False, applicable_seed


def _is_reviewed_seed(row, fragment):
    if not row.get("is_seed") or row.get("status") != "active" or row.get("pending") != []:
        return False
    from .quality_seed import ANSWER, QUESTION
    from backend.mailfaq.faq import _key

    return (fragment.version == 1 and fragment.text == ANSWER
            and row["question_key"] == _key(QUESTION) and row["conditions_key"] == "{}")


def _scope(fragments):
    """Check that every visible regulation belongs to the audited active source set."""
    documents = defaultdict(list)
    faq = []
    for fragment in fragments:
        if fragment.kind == "faq":
            faq.append((fragment.document_id.hex, fragment.revision_id.hex, fragment.fragment_id.hex, fragment.version))
            continue
        doc = DOC_BY_TITLE.get(fragment.title)
        if fragment.kind != "document" or doc is None:
            return None
        documents[doc].append(fragment)
    if documents.keys() != ORIGINALS.keys():
        return None
    versions = []
    refs = {}
    for doc, passages in sorted(documents.items()):
        content = sorted((p.anchor, p.text) for p in passages)
        version = passages[0].version
        if version == 1:
            original_hash, indexed_hash = ORIGINALS[doc]
        elif doc == "DB" and version == 2:
            original_hash, indexed_hash = DB_TRAINING_REVISION
        else:
            return None
        if _digest(content) != indexed_hash:
            return None
        document_id, revision_id = passages[0].document_id, passages[0].revision_id
        for passage in passages:
            if (passage.document_id != document_id or passage.revision_id != revision_id
                    or passage.version != version or passage.fragment_id != uuid5(revision_id, passage.anchor)):
                return None
            refs[doc, passage.anchor] = passage
        versions.append((doc, document_id, revision_id, original_hash))
    if len({version[1] for version in versions}) != len(ORIGINALS):
        return None
    return tuple(versions), faq, refs


def _official_originals(conn, versions):
    revisions = [version[2] for version in versions]
    rows = conn.execute("""
        SELECT s.external_id, s.id AS document_id, r.id AS revision_id, o.sha256
        FROM assembly_originals o JOIN source_revisions r ON r.id = o.revision_id
        JOIN sources s ON s.id = r.source_id
        WHERE r.id = ANY(%s::uuid[]) AND s.source_key = 'assembly-corpus'
    """, (revisions,)).fetchall()
    return {(row["external_id"], row["document_id"], row["revision_id"], row["sha256"]) for row in rows} == set(versions)


class VerifiedAnswers:
    def __init__(self, dsn, records):
        self.dsn = dsn
        self.records = records
        self._cache = OrderedDict()
        self._lock = Lock()

    def answer(self, request):
        if (request.conditions or request.history or request.clarification is not None
                or request.clarification_count or datetime.now(UTC) >= request.deadline):
            return None
        question = normalized(request.question).removesuffix("?").rstrip()
        estimates = _DB_ESTIMATE.fullmatch(question)
        rule = next((rule for rule in RULES if rule.question.fullmatch(question)), None)
        if rule is None and not estimates:
            return None
        scoped = _scope(request.sources.fragments)
        if scoped is None:
            return None
        versions, faq, refs = scoped
        if estimates:
            rule = _DB_ESTIMATE_RULES[refs["DB", "1.6"].version]
        # Standalone only. Keep fixed-size keys and omit raw question text and case data.
        key = POLICY_VERSION, _digest(request.question), versions, _digest(sorted(faq))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
        reviewed_faq = None
        if cached is None or faq:
            with connect(self.dsn) as conn:
                blocked, reviewed_faq = _check_faq(conn, request.question, request.conditions, request.sources.fragments)
                if blocked:
                    return None
                if cached is None and not _official_originals(conn, versions):
                    return None
        if cached is not None and reviewed_faq is None:
            return replace(cached, resolved_question=request.question)
        result = self._verify(rule, refs)
        if result is None:
            return None
        if reviewed_faq is not None:
            number = len(result.citations) + 1
            citation = Citation(reviewed_faq.document_id, reviewed_faq.revision_id, reviewed_faq.fragment_id,
                                reviewed_faq.text)
            return replace(result,
                           text=f"Регламент\n{result.text}\n\nFAQ Отдела качества\n{reviewed_faq.text} [{number}]"
                                "\n\nРуководствуйтесь регламентом. Повторно уточнить расхождение письмом?",
                           citations=(*result.citations, citation), resolved_question=request.question,
                           escalation_question=request.question)
        if result.action == "escalate":
            return replace(result, resolved_question=request.question, escalation_question=request.question)
        if result.action == "clarify":
            return replace(result, resolved_question=request.question)
        with self._lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return replace(result, resolved_question=request.question)

    def _verify(self, rule, refs):
        if rule.action == "clarify":
            choices = tuple(ClarificationOption(str(i), label, {"directorate": label})
                            for i, label in enumerate(DIRECTORATE_OPTIONS, 1))
            return VerifiedAnswer("clarify", rule.text, options=(*choices, ClarificationOption(
                "unknown", "Не знаю", {"directorate": "не знаю"})), clarification_key="directorate", allow_faq=False)
        by_id = {entry["id"]: entry for entry in self.records()}
        citations = []
        for claim_id, quote in rule.evidence:
            doc, ref, _ = claim_id.split(":")
            record = by_id.get(claim_id)
            part = refs[doc, ref]
            if (not record or record["status"] != "indexed" or (record["doc"], record["ref"]) != (doc, ref)
                    or record["source_sha256"] != ORIGINALS[doc][0]):
                return None
            original_text = record["quote"]
            if claim_id == "DB:1.6:1" and part.version == 2:
                original_text = original_text.replace(_DB_EXACT_ESTIMATE[1], _DB_EXACT_ESTIMATE[2])
            if original_text != part.text or quote not in part.text:
                return None
            citations.append(Citation(part.document_id, part.revision_id, part.fragment_id, quote))
        return VerifiedAnswer(rule.action, rule.text, citations=tuple(citations), allow_faq=False)
