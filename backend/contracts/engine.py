"""Read-only AnswerEngine v1 port and interchangeable synthetic implementations."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, Generic, Literal, Protocol, TypeVar
from uuid import UUID

from .fixtures import FixtureScenario, load_fixture

Action = Literal["answer", "clarify", "escalate", "out_of_scope"]
SourceKind = Literal["document", "faq"]


Value = TypeVar("Value")


class EngineUnavailable(Exception):
    """Technical failure before a trustworthy answer could be returned."""


@dataclass(frozen=True, slots=True)
class ModelAttemptResult(Generic[Value]):
    value: Value
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | int | None = None
    route: str | None = None
    model: str | None = None


class ModelAttemptFailure(EngineUnavailable):
    """Technical failure with usage or actual routing reported by the provider."""

    def __init__(
        self,
        reason: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost: Decimal | int | None = None,
        route: str | None = None,
        model: str | None = None,
    ):
        super().__init__(reason)
        self.result = ModelAttemptResult(None, input_tokens, output_tokens, cost, route, model)


class ModelAttemptRecorder(Protocol):
    def call(
        self,
        invoke: Callable[[], Value | ModelAttemptResult[Value]],
        *,
        role: str,
        model: str,
        route: str,
        trace_id: str | None = None,
    ) -> Value:
        """Invoke and record one physical provider attempt, including retries or judge calls."""
        ...


@dataclass(frozen=True, slots=True)
class SourceFragment:
    document_id: UUID
    revision_id: UUID
    fragment_id: UUID
    text: str
    title: str = ""
    anchor: str = ""
    kind: SourceKind = "document"
    version: int | None = None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """The active, permitted fragments selected by the application."""

    fragments: tuple[SourceFragment, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "fragments", tuple(self.fragments))


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: Literal["user", "assistant", "system"]
    text: str
    question: str | None = None
    resolved: str | None = None
    action: Action | None = None


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    question: str
    turn_id: UUID
    case_id: UUID
    conditions: Mapping[str, str]
    history: tuple[HistoryMessage, ...]
    deadline: datetime
    sources: SourceSnapshot
    attempts: ModelAttemptRecorder | None = None
    clarification: Mapping | None = None
    clarification_count: int = 0

    def __post_init__(self):
        if self.deadline.utcoffset() is None:
            raise ValueError("The engine deadline must include a time zone")
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))
        object.__setattr__(self, "history", tuple(self.history))


@dataclass(frozen=True, slots=True)
class ClarificationOption:
    id: str
    text: str
    conditions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: UUID
    revision_id: UUID
    fragment_id: UUID
    quote: str
    claim: str | None = None
    # Exact name in the quotation whose published link the application should resolve.
    linked_document: str | None = None


@dataclass(frozen=True, slots=True)
class AnswerPart:
    action: Action
    text: str
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerResult:
    action: Action
    text: str
    options: tuple[ClarificationOption, ...] = ()
    citations: tuple[Citation, ...] = ()
    clarification_key: str | None = None
    parts: tuple[AnswerPart, ...] = ()
    escalation_question: str | None = None
    resolved_question: str | None = None
    allow_faq: bool = True

    def __post_init__(self):
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "citations", tuple(self.citations))


class AnswerEngine(Protocol):
    version: ClassVar[int] = 1

    def answer(self, request: AnswerRequest) -> AnswerResult:
        """Return an action or raise EngineUnavailable for technical failure."""
        ...


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


class _FixtureEngine:
    version = 1

    def __init__(self):
        self.scenarios = load_fixture().scenarios

    def _find(self, question: str) -> FixtureScenario | None:
        raise NotImplementedError

    def answer(self, request: AnswerRequest) -> AnswerResult:
        if datetime.now(UTC) >= request.deadline:
            raise EngineUnavailable("Engine deadline exceeded")

        scenario = self._find(_normalize(request.question))
        if scenario is None:
            return AnswerResult("out_of_scope", "В учебных сценариях такого вопроса нет.")
        if scenario.action == "technical_failure":
            raise EngineUnavailable("Simulated connection failure")
        if scenario.action == "escalate":
            return AnswerResult("escalate", scenario.text)

        if scenario.condition_key and scenario.condition_key not in request.conditions:
            return AnswerResult(
                "clarify",
                scenario.text,
                options=tuple(
                    ClarificationOption(option.id, option.text, {scenario.condition_key: option.id})
                    for option in scenario.options
                ),
                clarification_key=scenario.condition_key,
            )

        fragment = self._select_fragment(request, scenario.document_id)
        if fragment is None:
            return AnswerResult("escalate", "В разрешённых источниках нет ответа.")
        return AnswerResult(
            "answer",
            fragment.text,
            citations=(Citation(
                document_id=fragment.document_id,
                revision_id=fragment.revision_id,
                fragment_id=fragment.fragment_id,
                quote=fragment.text,
                claim=fragment.text,
            ),),
        )

    @staticmethod
    def _select_fragment(request: AnswerRequest, document_id: UUID | None) -> SourceFragment | None:
        return next((fragment for fragment in request.sources.fragments
                     if fragment.document_id == document_id), None)


class FixtureAlphaEngine(_FixtureEngine):
    model = "fixture-alpha-v1"
    route = "synthetic/alpha"

    def __init__(self):
        super().__init__()
        self.by_question = {
            _normalize(question): scenario
            for scenario in self.scenarios
            for question in scenario.questions
        }

    def _find(self, question: str) -> FixtureScenario | None:
        return self.by_question.get(question)


class FixtureBetaEngine(_FixtureEngine):
    model = "fixture-beta-v1"
    route = "synthetic/beta"

    def _find(self, question: str) -> FixtureScenario | None:
        return next((
            scenario for scenario in self.scenarios
            if any(_normalize(alias) == question for alias in scenario.questions)
        ), None)


def build_engine(name: str) -> AnswerEngine:
    if name == "fixture-alpha":
        return FixtureAlphaEngine()
    if name == "fixture-beta":
        return FixtureBetaEngine()
    raise ValueError(f"Unknown AnswerEngine: {name}")
