"""Opt-in physical model journal smoke test, using only a synthetic prompt."""

from decimal import Decimal
import json
import os
from uuid import uuid4

import httpx
import pytest

from backend.db import connect
from backend.runtime import ModelResult, call_model


@pytest.mark.skipif(os.getenv("II_LIVE_MODEL") != "1", reason="Live provider run is opt-in")
def test_one_physical_model_attempt(dsn):
    model, provider = "google/gemma-4-26b-a4b-it", "Makora"
    trace = str(uuid4())

    def call():
        with httpx.Client(timeout=20, follow_redirects=False) as http:
            response = http.post("https://openrouter.ai/api/v1/chat/completions",
                                 headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]},
                                 json={"model": model, "provider": {"order": [provider], "allow_fallbacks": False},
                                       "max_tokens": 8, "messages": [{"role": "user", "content": "Reply OK."}]})
            response.raise_for_status()
            data = json.loads(response.content, parse_float=Decimal)
        usage = data["usage"]
        assert data.get("provider"), "The provider must report the actual route"
        return ModelResult(data["choices"], input_tokens=usage.get("prompt_tokens"),
                           output_tokens=usage.get("completion_tokens"), cost=usage.get("cost"),
                           model=data["model"], route=data["provider"])

    choices = call_model(dsn, call, role="diagnostic", trace_id=trace, model=model, route=provider)
    assert choices
    with connect(dsn) as conn:
        row = conn.execute("SELECT state,model,route,input_tokens,output_tokens,cost FROM model_calls WHERE trace_id=%s",
                           (trace,)).fetchone()
    assert row["state"] == "succeeded" and row["route"].casefold() == provider.casefold()
    assert row["input_tokens"] is not None and row["output_tokens"] is not None
    print("MODEL JOURNAL", json.dumps(row, default=str))
