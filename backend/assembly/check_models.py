"""Check each configured local model via the application transport using synthetic inputs."""
import argparse
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

from backend.contracts.engine import EngineUnavailable
from eval.decider.policy import GEMMA, MODEL as DECIDER
from eval.search_core.core import QWEN
from .model_profile import ModelConfigurationError, ROLES, load_model_profile
from .models import ModelCalls


def check(role):
    calls = ModelCalls(SimpleNamespace(deadline=datetime.now(UTC) + timedelta(seconds=15), attempts=None))
    if role == "decider":
        questions = {
            "status": {"type": "choice", "instructions": "Select the status present in the sample.",
                       "criteria": {"ready": "The sample is ready.", "other": "Any other value."}},
            "grounded": {"type": "noul", "instructions": "Is the sample's status explicitly given?"},
        }
        result = calls.decisions({"model": DECIDER, "state": '{"status":"ready"}', "questions": questions})
        return result["response"].get("model")
    model = {"gemma": GEMMA, "qwen": QWEN}[role]
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": True, "stream_options": {"include_usage": True}, "temperature": 0, "max_tokens": 32}
    result = calls.chat("model-check", body, "deployment-check")
    return result.get("served_model")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true", help="Validate all local settings without making any requests")
    args = parser.parse_args()
    try:
        profile = load_model_profile()
        if not profile.is_local:
            raise ModelConfigurationError("Set II_MODEL_PROFILE=local to check internal services")
    except ModelConfigurationError as error:
        parser.exit(2, f"Configuration error: {error}\n")

    failed = False
    for role in ROLES:
        target = profile.target(role)
        row = {"role": role, "host": urlsplit(target.url).netloc, "configured_model": target.model_id}
        if args.config_only:
            row["status"] = "configured"
        else:
            try:
                row["served_model"] = check(role)
                if row["served_model"]:
                    row["status"] = "ok"
                else:
                    row.update(status="unverified", error="The service did not report its model ID")
                    failed = True
            except EngineUnavailable as error:
                row.update(status="failed", error=str(error))
                failed = True
        print(json.dumps(row, ensure_ascii=False))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
