"""OpenRouter requests with a deadline covering headers and the entire body."""
import asyncio
import os
from time import monotonic

import httpx

from eval.decider.policy import ENDPOINT as DECISIONS
from eval.openrouter import ENDPOINT as CHAT, chat_row, finish_chat, read_chat_line

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


class UpstreamFailure(Exception):
    def __init__(self, reason, *, retryable=False):
        super().__init__(reason)
        self.retryable = retryable
        self.row = None


async def _request(endpoint, body, timeout, row=None, stream_timeout=None):
    headers = {"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]}
    if row is not None:
        headers["Accept"] = "text/event-stream"
    started = monotonic()
    try:
        loop = asyncio.get_running_loop()
        deadline = asyncio.timeout_at(loop.time() + timeout)
        stream_end = loop.time() + stream_timeout if stream_timeout is not None else None
        async with deadline, httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", endpoint, headers=headers, json=body) as response:
                if row is not None:
                    row["http"] = response.status_code
                if not response.is_success:
                    raise UpstreamFailure(f"OpenRouter HTTP {response.status_code}",
                                          retryable=response.status_code in RETRYABLE_STATUS)
                if row is None:
                    await response.aread()
                    return response.json()
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise UpstreamFailure("OpenRouter did not return an event stream", retryable=True)
                async for line in response.aiter_lines():
                    done = read_chat_line(row, line.encode(), started)
                    if stream_end is not None and row["ttft_s"] is not None:
                        # Only the first non-whitespace content earns the remaining
                        # reading time. Keep-alives and later chunks never reset it.
                        deadline.reschedule(stream_end)
                        stream_end = None
                    if done:
                        finish_chat(row, True)
                        if not row["ok"]:
                            raise UpstreamFailure("OpenRouter returned an incomplete answer")
                        return row
                raise UpstreamFailure("OpenRouter closed the answer stream early", retryable=True)
    except TimeoutError:
        raise UpstreamFailure("OpenRouter request exceeded its deadline", retryable=True) from None
    except httpx.RequestError:
        raise UpstreamFailure("OpenRouter connection interrupted", retryable=True) from None
    except (AttributeError, TypeError, ValueError):
        code = row.get("stream_error_code") if row is not None else None
        raise UpstreamFailure("OpenRouter returned an invalid response",
                              retryable=isinstance(code, int) and code in RETRYABLE_STATUS) from None


def complete(case, body, stage, *, timeout, stream_timeout=None):
    row = chat_row(case, body, stage)
    started = monotonic()
    try:
        return asyncio.run(_request(CHAT, body, timeout, row, stream_timeout))
    except UpstreamFailure as error:
        error.row = row
        raise
    finally:
        row["latency_s"] = round(monotonic() - started, 3)


def decide(body, *, timeout):
    started = monotonic()
    response = asyncio.run(_request(DECISIONS, body, timeout))
    if not isinstance(response, dict):
        raise UpstreamFailure("OpenRouter Decisions returned an invalid response")
    return {"response": response, "http": 200, "usage": response.get("usage") or {},
            "latency_s": round(monotonic() - started, 3)}
