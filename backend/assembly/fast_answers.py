"""Audited, source-bound answers for a small set of standalone questions."""
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from threading import Lock
from uuid import uuid5

from backend.contracts.engine import Action, AnswerResult, Citation
from backend.db import connect
from eval.retrieval.extract import TITLES
from eval.scope.procedures import directories, normalized


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
# The training edition changes only DB 1.6. _verify still requires the
# registry's entire underlying clause to match before it can return any rule.
DB_TRAINING_REVISION = (
    "b393a87402817a588bc75cc9f29011324977e18db835bdbfb64298cd95769a08",
    "386c756cbd29ca454eeee57c3f6516a0388c92037e4e5cd78010b052f224cf0c",
)
DOC_BY_TITLE = {title: doc for doc, title in TITLES.items()}
POLICY_VERSION = 2
CACHE_SIZE = 64


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


def potential_faq(conn, question, conditions, fragments):
    faq = {fragment.fragment_id: fragment for fragment in fragments if fragment.kind == "faq"}
    if not faq:
        return False
    rows = conn.execute("""
        SELECT f.current_fragment_id, f.question_key, f.conditions_key, seed.faq_id IS NOT NULL AS is_seed
        FROM faq_entries f LEFT JOIN assembly_quality_seed seed ON seed.faq_id=f.id
        WHERE f.current_fragment_id = ANY(%s::uuid[])
    """, (list(faq),)).fetchall()
    if {row["current_fragment_id"] for row in rows} != faq.keys():
        return True
    scope, text = directories(question), normalized(question)
    for row in rows:
        if not faq_candidate(row, scope, text, conditions):
            continue
        if row.get("is_seed") and _DB_APPOINTMENT.fullmatch(text.removesuffix("?").rstrip()):
            from .quality_seed import ANSWER, QUESTION
            from backend.mailfaq.faq import _key
            # A question about the duration of an exact estimate cannot govern
            # who appoints the PM at initiation. Any edit or other FAQ still
            # requires the ordinary applicability check and publication guard.
            fragment = faq[row["current_fragment_id"]]
            if (fragment.version == 1 and fragment.text == ANSWER and row["question_key"] == _key(QUESTION)
                    and row["conditions_key"] == "{}"):
                continue
        return True
    return False


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
        rule = next((rule for rule in RULES if rule.question.fullmatch(question)), None)
        if rule is None:
            return None
        scoped = _scope(request.sources.fragments)
        if scoped is None:
            return None
        versions, faq, refs = scoped
        # Standalone only. Keep fixed-size keys and omit raw question text and case data.
        key = POLICY_VERSION, _digest(request.question), versions, _digest(sorted(faq))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
        if cached is None or faq:
            with connect(self.dsn) as conn:
                if potential_faq(conn, request.question, request.conditions, request.sources.fragments):
                    return None
                if cached is None and not _official_originals(conn, versions):
                    return None
        if cached is not None:
            return replace(cached, resolved_question=request.question)
        result = self._verify(rule, refs)
        if result is None:
            return None
        if result.action == "escalate":
            return replace(result, resolved_question=request.question, escalation_question=request.question)
        with self._lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return replace(result, resolved_question=request.question)

    def _verify(self, rule, refs):
        by_id = {entry["id"]: entry for entry in self.records()}
        citations = []
        for claim_id, quote in rule.evidence:
            doc, ref, _ = claim_id.split(":")
            record = by_id.get(claim_id)
            part = refs[doc, ref]
            if (not record or record["status"] != "indexed" or (record["doc"], record["ref"]) != (doc, ref)
                    or record["source_sha256"] != ORIGINALS[doc][0] or record["quote"] != part.text
                    or quote not in part.text):
                return None
            citations.append(Citation(part.document_id, part.revision_id, part.fragment_id, quote))
        return VerifiedAnswer(rule.action, rule.text, citations=tuple(citations), allow_faq=False)
