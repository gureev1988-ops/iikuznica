"""Physical calls to the selected OpenRouter chat and Decisions routes."""
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
import time
from urllib.error import HTTPError

from backend.contracts.engine import EngineUnavailable, ModelAttemptFailure, ModelAttemptResult
from eval.openrouter import complete
from eval.decider.run import decide
from eval.decider.policy import GEMMA, MODEL as DECIDER
from eval.search_core.core import QWEN, model_settings

PROVIDERS = {GEMMA: "deepinfra/fp8", QWEN: "alibaba"}


class ModelCalls:
    def __init__(self, request):
        self.request = request

    def remaining(self):
        return (self.request.deadline - datetime.now(UTC)).total_seconds()

    def chat(self, case, body, stage):
        model = body["model"]
        body = model_settings(body, model, PROVIDERS[model], min(body.get("max_tokens", 1536), 8192))

        def invoke(timeout):
            row = complete(case, body, stage, timeout=timeout)
            if not row["ok"]:
                raise ModelAttemptFailure(f"Chat failed (HTTP {row['http']})", **self.usage(row, "chat"))
            return ModelAttemptResult(row, **self.usage(row, "chat"))

        return self.call(invoke, stage, model, f"openrouter/chat/{PROVIDERS[model]}")

    def decisions(self, body):
        def invoke(timeout):
            try:
                result = decide(body, timeout=timeout)
            except (HTTPError, OSError, ValueError) as exc:
                code = getattr(exc, "code", None)
                raise ModelAttemptFailure(f"Decisions failed (HTTP {code})") from None
            reply = result["response"]
            if not isinstance(reply.get("answers"), dict):
                raise ModelAttemptFailure("Decisions returned no answers", **self.usage(reply, "decisions"))
            return ModelAttemptResult(result, **self.usage(reply, "decisions"))

        return self.call(invoke, "judge", DECIDER, "openrouter/decisions/perplexity")

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

    def call(self, invoke, role, model, route):
        if not os.getenv("OPENROUTER_API_KEY"):
            raise EngineUnavailable("OPENROUTER_API_KEY is missing")
        for attempt in range(2):
            remaining = self.remaining()
            if remaining <= 0:
                raise EngineUnavailable("Engine deadline exceeded")
            try:
                perform = lambda: invoke(min(remaining, 45))
                if self.request.attempts is not None:
                    return self.request.attempts.call(perform, role=role, model=model, route=route)
                return perform().value
            except ModelAttemptFailure as exc:
                if attempt or self.remaining() < 2 or not any(f"HTTP {n}" in str(exc) for n in (429, 502, 503, 504)):
                    raise
                time.sleep(0.75)


def json_reply(row):
    raw = row["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a structured model response")
    return value
