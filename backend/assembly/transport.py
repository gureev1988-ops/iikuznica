"""Deadline-bound chat streams and structured decisions for the active model profile."""
import asyncio
import os
import ssl
from time import monotonic

import httpx

from eval.decider.policy import ENDPOINT as DECISIONS, MODEL as DECIDER
from eval.openrouter import ENDPOINT as CHAT, SEARCH_MODELS, chat_row, finish_chat, read_chat_line
from .model_profile import load_model_profile

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}
MODEL_ROLES = {SEARCH_MODELS[0]: "gemma", SEARCH_MODELS[1]: "qwen", DECIDER: "decider"}


class UpstreamFailure(Exception):
    def __init__(self, reason, *, retryable=False):
        super().__init__(reason)
        self.retryable = retryable
        self.row = None


async def _request(endpoint, body, timeout, *, token, local, row=None, stream_timeout=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    if row is not None:
        headers["Accept"] = "text/event-stream"
    client_options = {"timeout": None, "follow_redirects": False}
    if local:
        client_options["trust_env"] = False
        if endpoint.lower().startswith("https://"):
            try:
                client_options["verify"] = ssl.create_default_context(cafile=os.getenv("SSL_CERT_FILE") or None)
            except OSError:
                raise UpstreamFailure("Invalid local model TLS trust configuration") from None
    service = "Local model" if local else "OpenRouter"
    started = monotonic()
    try:
        loop = asyncio.get_running_loop()
        deadline = asyncio.timeout_at(loop.time() + timeout)
        stream_end = loop.time() + stream_timeout if stream_timeout is not None else None
        async with deadline, httpx.AsyncClient(**client_options) as client:
            async with client.stream("POST", endpoint, headers=headers, json=body) as response:
                if row is not None:
                    row["http"] = response.status_code
                if not response.is_success:
                    raise UpstreamFailure(f"{service} HTTP {response.status_code}",
                                          retryable=response.status_code in RETRYABLE_STATUS)
                if row is None:
                    await response.aread()
                    return response.json()
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise UpstreamFailure(f"{service} did not return an event stream", retryable=True)
                async for line in response.aiter_lines():
                    done = read_chat_line(row, line.encode(), started)
                    if local and row["served_model"] is not None and row["served_model"] != body["model"]:
                        raise UpstreamFailure("Local chat reported a different model ID")
                    if stream_end is not None and row["ttft_s"] is not None:
                        # Only the first non-whitespace content earns the remaining
                        # reading time. Keep-alives and later chunks never reset it.
                        deadline.reschedule(stream_end)
                        stream_end = None
                    if done:
                        finish_chat(row, True)
                        if not row["ok"]:
                            raise UpstreamFailure(f"{service} returned an incomplete answer")
                        return row
                raise UpstreamFailure(f"{service} closed the answer stream early", retryable=True)
    except TimeoutError:
        raise UpstreamFailure(f"{service} request exceeded its deadline", retryable=True) from None
    except httpx.RequestError:
        raise UpstreamFailure(f"{service} connection interrupted", retryable=True) from None
    except (AttributeError, TypeError, ValueError):
        code = row.get("stream_error_code") if row is not None else None
        raise UpstreamFailure(f"{service} returned an invalid response",
                              retryable=isinstance(code, int) and code in RETRYABLE_STATUS) from None


def complete(case, body, stage, *, timeout, stream_timeout=None):
    profile = load_model_profile()
    if profile.is_local:
        role = MODEL_ROLES.get(body.get("model"))
        if role not in ("gemma", "qwen"):
            raise UpstreamFailure("Unknown local chat role")
        target = profile.target(role)
        endpoint, token = target.url, target.token
        # Hosted routing/decoder controls are not part of the OpenAI-compatible
        # chat contract. The local server sets its model-specific decoding mode.
        body = {k: body[k] for k in ("messages", "temperature", "max_tokens", "stop", "top_p") if k in body}
        body.update(model=target.model_id, stream=True, stream_options={"include_usage": True})
    else:
        endpoint, token = CHAT, os.environ.get("OPENROUTER_API_KEY")
        if not token:
            raise UpstreamFailure("OPENROUTER_API_KEY is missing")
    row = chat_row(case, body, stage)
    started = monotonic()
    try:
        return asyncio.run(_request(endpoint, body, timeout, row=row, stream_timeout=stream_timeout,
                                    token=token, local=profile.is_local))
    except UpstreamFailure as error:
        error.row = row
        raise
    finally:
        row["latency_s"] = round(monotonic() - started, 3)


def decide(body, *, timeout):
    profile = load_model_profile()
    if profile.is_local:
        if body.get("model") != DECIDER:
            raise UpstreamFailure("Unknown local decisions role")
        target = profile.target("decider")
        endpoint, token = target.url, target.token
        body = {"model": target.model_id, "questions": body["questions"], "state": body["state"]}
    else:
        endpoint, token = DECISIONS, os.environ.get("OPENROUTER_API_KEY")
        if not token:
            raise UpstreamFailure("OPENROUTER_API_KEY is missing")
    started = monotonic()
    response = asyncio.run(_request(endpoint, body, timeout, token=token, local=profile.is_local))
    if not isinstance(response, dict):
        raise UpstreamFailure("Decisions returned an invalid response")
    if profile.is_local and response.get("model") is not None and response["model"] != body["model"]:
        error = UpstreamFailure("Local decisions reported a different model ID")
        error.row = response
        raise error
    return {"response": response, "http": 200, "usage": response.get("usage") or {},
            "latency_s": round(monotonic() - started, 3)}
