"""Physical chat and Decisions attempts selected for an A1 request."""
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit

from backend.contracts.engine import EngineUnavailable, ModelAttemptFailure, ModelAttemptResult
from backend.runtime.jobs import PermanentJobFailure
from eval.decider.policy import GEMMA, MODEL as DECIDER
from eval.search_core.core import QWEN, model_settings
from .model_profile import load_model_profile
from .pricing import charge
from .transport import MODEL_ROLES, UpstreamFailure, complete, decide

PROVIDERS = {GEMMA: "deepinfra/fp8", QWEN: "alibaba"}
RESERVE_ENV = {GEMMA: "II_GEMMA_RESERVE_PROVIDER", QWEN: "II_QWEN_RESERVE_PROVIDER"}
ATTEMPT_SECONDS = {GEMMA: (3, 3.5), QWEN: (5, 5), DECIDER: (2.5, 3)}
EVIDENCE_STREAM_SECONDS = 6.5
LATENCY_LOG = logging.getLogger("backend.assembly.latency")


class AssemblyModelFailure(ModelAttemptFailure, PermanentJobFailure):
    """Allow stage failover without repeating an entire answer job."""

    def __init__(self, reason, *, retryable=False, **usage):
        super().__init__(reason, **usage)
        self.retryable = retryable


class ModelCalls:
    def __init__(self, request):
        self.request = request
        self.profile = load_model_profile()

    def remaining(self):
        return (self.request.deadline - datetime.now(UTC)).total_seconds()

    def chat(self, case, body, stage):
        model = body["model"]
        if model not in PROVIDERS:
            raise EngineUnavailable("Unknown chat model role")
        if self.profile.is_local:
            providers = (MODEL_ROLES[model], MODEL_ROLES[model])
        else:
            primary = PROVIDERS[model]
            providers = (primary, os.getenv(RESERVE_ENV[model], "").strip() or primary)

        def invoke(timeout, provider):
            limit = min(body.get("max_tokens", 1536), 8192)
            payload = dict(body, max_tokens=limit) if self.profile.is_local else model_settings(body, model, provider, limit)
            stream_timeout = None
            compound = stage in {"evidence-core", "permission-review"} and re.search(
                r"\?\s+[^?\s][^?]*\?", getattr(self.request, "question", "")
            )
            if model == GEMMA and (compound or stage == "faq-applicability"):
                # Both evidence selection and FAQ comparison can stream grounded details
                # after a timely first token. Keep acquisition and total turn bounded.
                stream_timeout = min(EVIDENCE_STREAM_SECONDS, self.remaining() - 0.15)
                stream_timeout = max(timeout, stream_timeout)
            try:
                row = complete(case, payload, stage, timeout=timeout, stream_timeout=stream_timeout)
            except UpstreamFailure as error:
                snapshot = error.row or {}
                self.observe(stage, model, "chat", provider, snapshot, "failed")
                raise AssemblyModelFailure(str(error), retryable=error.retryable,
                                           **self.usage(snapshot, "chat", model, provider)) from None
            self.observe(stage, model, "chat", provider, row, "succeeded")
            return ModelAttemptResult(row, **self.usage(row, "chat", model, provider))

        return self.call(invoke, stage, model, "chat", providers)

    def decisions(self, body):
        def invoke(timeout, provider):
            started = time.monotonic()
            try:
                result = decide(body, timeout=timeout)
            except UpstreamFailure as error:
                snapshot = {**(error.row or {}), "latency_s": time.monotonic() - started}
                self.observe("judge", DECIDER, "decisions", provider, snapshot, "failed")
                raise AssemblyModelFailure(str(error), retryable=error.retryable,
                                           **self.usage(snapshot, "decisions", DECIDER, provider)) from None
            reply = result["response"]
            if reply.get("error") or reply.get("refusal") or not decisions_valid(reply.get("answers"), body["questions"]):
                self.observe("judge", DECIDER, "decisions", provider, {**reply, "latency_s": result["latency_s"]}, "failed")
                raise AssemblyModelFailure("Decisions returned a malformed answer", **self.usage(reply, "decisions", DECIDER, provider))
            self.observe("judge", DECIDER, "decisions", provider, {**reply, "latency_s": result["latency_s"]}, "succeeded")
            return ModelAttemptResult(result, **self.usage(reply, "decisions", DECIDER, provider))

        provider = "decider" if self.profile.is_local else "perplexity"
        return self.call(invoke, "judge", DECIDER, "decisions", (provider, provider))

    def destination(self, model, endpoint, provider):
        if self.profile.is_local:
            role = MODEL_ROLES[model]
            target = self.profile.target(role)
            return target.model_id, f"local/{endpoint}/{role}@{urlsplit(target.url).netloc}"
        return model, f"openrouter/{endpoint}/{provider}"

    def observe(self, stage, model, endpoint, provider, row, state):
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details") or {}
        requested, route = self.destination(model, endpoint, provider)
        reported = row.get("model") if endpoint == "decisions" else row.get("served_model")
        LATENCY_LOG.info(
            "turn=%s stage=%s requested_model=%s served_model=%s route=%s provider=%s state=%s elapsed_s=%s "
            "ttft_s=%s output_tokens=%s reasoning_tokens=%s",
            getattr(self.request, "turn_id", None), stage, requested, reported, route, row.get("provider"),
            state, row.get("latency_s"), row.get("ttft_s"),
            usage.get("completion_tokens", usage.get("output_tokens")),
            details.get("reasoning_tokens") if isinstance(details, dict) else None,
        )

    def usage(self, row, endpoint, requested, tag):
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        cost = usage.get("cost")
        provider = row.get("provider")
        served = row.get("model") if endpoint == "decisions" else row.get("served_model")
        if cost is None and not self.profile.is_local:
            if (served or requested) == DECIDER and DECIDER.endswith(":free") and usage:
                cost = Decimal(0)
            elif (served or requested) == requested:
                cost = charge(requested, tag, provider, usage)
        try:
            cost = Decimal(str(cost)) if cost is not None else None
            if cost is not None and (not cost.is_finite() or cost < 0):
                cost = None
        except InvalidOperation:
            cost = None
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        return dict(input_tokens=input_tokens if type(input_tokens) is int and input_tokens >= 0 else None,
                    output_tokens=output_tokens if type(output_tokens) is int and output_tokens >= 0 else None,
                    cost=cost, model=served if isinstance(served, str) else None,
                    route=f"openrouter/{endpoint}/{provider}" if provider and not self.profile.is_local else None)

    def call(self, invoke, role, model, endpoint, providers):
        if not self.profile.is_local and not os.getenv("OPENROUTER_API_KEY"):
            raise EngineUnavailable("OPENROUTER_API_KEY is missing")
        # A cold-ranking failure can use the original local MiniLM without a second external attempt.
        limits = (2.0,) if role == "passage-ranking" else ATTEMPT_SECONDS[model]
        for attempt, (provider, limit) in enumerate(zip(providers, limits)):
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
                    requested, route = self.destination(model, endpoint, provider)
                    return self.request.attempts.call(perform, role=role, model=requested, route=route)
                return perform().value
            except AssemblyModelFailure as error:
                if attempt + 1 == len(limits) or not error.retryable or self.remaining() < 0.6:
                    raise
                time.sleep(0.1)


def probability(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def decisions_valid(answers, questions):
    if not isinstance(answers, dict):
        return False
    for key, question in questions.items():
        value = answers.get(key)
        if (not isinstance(value, dict) or value.get("type") != question["type"]
                or value.get("type") not in ("noul", "choice") or value.get("refusal")):
            return False
        if value["type"] == "noul" and not probability(value.get("noul")):
            return False
        if value["type"] == "choice":
            scores = value.get("probabilities")
            if (not isinstance(scores, dict) or not isinstance(value.get("choice"), str)
                    or value["choice"] not in question["criteria"]
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
