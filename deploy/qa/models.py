"""Run one physical call per selected role inside the interactive container."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from backend.assembly.models import DECIDER, GEMMA, QWEN, ModelCalls


def main():
    calls = ModelCalls(SimpleNamespace(deadline=datetime.now(UTC) + timedelta(seconds=80), attempts=None))
    for model in (GEMMA, QWEN):
        answer = calls.chat("smoke", {"model": model, "messages": [{"role": "user", "content": "Reply OK."}],
                                     "stream": True, "stream_options": {"include_usage": True},
                                     "max_tokens": 16}, "smoke")
        print(answer["http"], answer["served_model"], answer["provider"], answer["usage"])
    result = calls.decisions({
        "model": DECIDER, "state": "The sky is blue.",
        "questions": {"check": {"type": "choice", "criteria": {"yes": "Sky is blue", "no": "Sky is red"}}},
    })
    response = result["response"]
    assert response["answers"]["check"]["choice"] == "yes"
    print(result["http"], response["model"], response["provider"], response["usage"])


if __name__ == "__main__":
    main()
