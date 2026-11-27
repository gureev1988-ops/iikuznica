"""Physical calls to the selected OpenRouter chat and Decisions routes."""
from datetime import UTC, datetime
from decimal import Decimal
import json
import logging
import math
import os
import time

from backend.contracts.engine import EngineUnavailable, ModelAttemptFailure, ModelAttemptResult
from backend.runtime.jobs import PermanentJobFailure
from eval.decider.policy import GEMMA, MODEL as DECIDER
from eval.search_core.core import QWEN, model_settings
from .transport import UpstreamFailure, complete, decide

PROVIDERS = {GEMMA: "deepinfra/fp8", QWEN: "alibaba"}
RESERVE_ENV = {GEMMA: "II_GEMMA_RESERVE_PROVIDER", QWEN: "II_QWEN_RESERVE_PROVIDER"}
ATTEMPT_SECONDS = {GEMMA: (3, 3.5), QWEN: (5, 5), DECIDER: (2.5, 3)}
LATENCY_LOG = logging.getLogger("backend.assembly.latency")


class AssemblyModelFailure(ModelAttemptFailure, PermanentJobFailure):
    """Allow stage failover without repeating an entire answer job."""

    def __init__(self, reason, *, retryable=False, **usage):
        super().__init__(reason, **usage)
        self.retryable = retryable


class ModelCalls:
    def __init__(self, request):
        self.request = request

    def remaining(self):
        return (self.request.deadline - datetime.now(UTC)).total_seconds()

    def chat(self, case, body, stage):
        model = body["model"]
        primary = PROVIDERS[model]
        reserve = os.getenv(RESERVE_ENV[model], "").strip() or primary

        def invoke(timeout, provider):
            payload = model_settings(body, model, provider, min(body.get("max_tokens", 1536), 8192))
            try:
                row = complete(case, payload, stage, timeout=timeout)
            except UpstreamFailure as error:
                snapshot = error.row or {}
                self.observe(stage, model, provider, snapshot, "failed")
                raise AssemblyModelFailure(str(error), retryable=error.retryable,
                                           **self.usage(snapshot, "chat")) from None
            self.observe(stage, model, provider, row, "succeeded")
            return ModelAttemptResult(row, **self.usage(row, "chat"))

        return self.call(invoke, stage, model, "chat", (primary, reserve))

    def decisions(self, body):
        def invoke(timeout, provider):
            started = time.monotonic()
            try:
                result = decide(body, timeout=timeout)
            except UpstreamFailure as error:
                self.observe("judge", DECIDER, provider, {"latency_s": time.monotonic() - started}, "failed")
                raise AssemblyModelFailure(str(error), retryable=error.retryable) from None
            reply = result["response"]
            if not decisions_valid(reply.get("answers"), body["questions"]):
                self.observe("judge", DECIDER, provider, {**reply, "latency_s": result["latency_s"]}, "failed")
                raise AssemblyModelFailure("Decisions returned a malformed answer", **self.usage(reply, "decisions"))
            self.observe("judge", DECIDER, provider, {**reply, "latency_s": result["latency_s"]}, "succeeded")
            return ModelAttemptResult(result, **self.usage(reply, "decisions"))

        return self.call(invoke, "judge", DECIDER, "decisions", ("perplexity", "perplexity"))

    def observe(self, stage, model, provider, row, state):
        usage = row.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        LATENCY_LOG.info(
            "turn=%s stage=%s model=%s provider=%s state=%s elapsed_s=%s ttft_s=%s output_tokens=%s reasoning_tokens=%s",
            getattr(self.request, "turn_id", None), stage, row.get("served_model") or model,
            row.get("provider") or provider, state, row.get("latency_s"), row.get("ttft_s"),
            usage.get("completion_tokens", usage.get("output_tokens")), details.get("reasoning_tokens"),
        )

    @staticmethod
    def usage(row, endpoint):
        usage = row.get("usage") or {}
        cost = usage.get("cost")
        provider = row.get("provider")
        return dict(input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
                    output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
                    cost=Decimal(str(cost)) if cost is not None else None,
                    model=row.get("served_model") or row.get("model"),
                    route=f"openrouter/{endpoint}/{provider}" if provider else None)

    def call(self, invoke, role, model, endpoint, providers):
        if not os.getenv("OPENROUTER_API_KEY"):
            raise EngineUnavailable("OPENROUTER_API_KEY is missing")
        for attempt, (provider, limit) in enumerate(zip(providers, ATTEMPT_SECONDS[model])):
            def perform():
                # The recorder may take time before it starts the physical call.
                remaining = self.remaining() - 0.15
                if remaining <= 0:
                    raise EngineUnavailable("Engine deadline exceeded")
                return invoke(min(remaining, limit), provider)

            if self.remaining() <= 0.15:
                raise EngineUnavailable("Engine deadline exceeded")
            try:
                if self.request.attempts is not None:
                    return self.request.attempts.call(perform, role=role, model=model,
                                                      route=f"openrouter/{endpoint}/{provider}")
                return perform().value
            except AssemblyModelFailure as error:
                if attempt or not error.retryable or self.remaining() < 0.6:
                    raise
                time.sleep(0.1)


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def decisions_valid(answers, questions):
    if not isinstance(answers, dict):
        return False
    for key, question in questions.items():
        value = answers.get(key)
        if not isinstance(value, dict) or value.get("type") != question["type"]:
            return False
        if value["type"] == "noul" and not probability(value.get("noul")):
            return False
        if value["type"] == "choice":
            scores = value.get("probabilities")
            if (not isinstance(scores, dict) or value.get("choice") not in question["criteria"]
                    or not probability(value.get("confidence"))
                    or any(not probability(scores.get(option)) for option in question["criteria"])):
                return False
    return True

def json_reply(row):
    raw = row["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a structured model response")
    return value
