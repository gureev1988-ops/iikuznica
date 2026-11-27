"""The assembly entry points and background checks must use the selected deployment profile."""
from concurrent.futures import Future
from hashlib import sha256
from io import BytesIO
import logging
from types import SimpleNamespace
import sys

import pytest

from backend import __main__ as cli
from backend.assembly import artifacts
from backend.assembly.model_profile import ModelConfigurationError, load_model_profile


@pytest.fixture
def local_models(monkeypatch):
    monkeypatch.setenv("II_MODEL_PROFILE", "local")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for role in ("GEMMA", "QWEN", "DECIDER"):
        monkeypatch.setenv(f"II_LOCAL_{role}_MODEL", f"{role.lower()}-fixture")
        monkeypatch.setenv(f"II_LOCAL_{role}_URL", f"http://{role.lower()}.internal/v1/inference")


@pytest.fixture
def assembly_cli(monkeypatch):
    latency = logging.getLogger("backend.assembly.latency")
    monkeypatch.setattr(latency, "handlers", [])
    monkeypatch.setattr(latency, "level", latency.level)
    monkeypatch.setattr(latency, "propagate", latency.propagate)
    monkeypatch.setenv("II_PROFILE", "assembly")
    monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1/unused")
    monkeypatch.delenv("II_CONFIG", raising=False)
    monkeypatch.setenv("II_JUDGE_LOGIN", "operator")
    for name in ("II_BOT_PASSWORD", "II_QUALITY_PASSWORD", "II_JUDGE_PASSWORD"):
        monkeypatch.setenv(name, "assembly-testing-password")


@pytest.mark.parametrize("mode", ["api", "interactive", "background", "assembly-init"])
def test_incomplete_local_profile_aborts_before_start(assembly_cli, local_models, monkeypatch, mode):
    monkeypatch.delenv("II_LOCAL_QWEN_MODEL")
    monkeypatch.setattr(sys, "argv", ["backend", mode])
    with pytest.raises(ModelConfigurationError, match="II_LOCAL_QWEN_MODEL"):
        cli.main()


@pytest.mark.parametrize("local", [True, False], ids=["pilot", "demo"])
def test_assembly_api_can_start_without_openrouter_key(assembly_cli, local_models, monkeypatch, local):
    import uvicorn

    if not local:
        monkeypatch.delenv("II_MODEL_PROFILE")
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(app))
    monkeypatch.setattr(sys, "argv", ["backend", "api"])
    cli.main()
    assert len(served) == 1


@pytest.mark.parametrize("local", [True, False], ids=["pilot", "demo"])
def test_interactive_worker_only_requires_demo_key_for_openrouter(assembly_cli, local_models, monkeypatch, local):
    from backend.assembly.search import Search

    if not local:
        monkeypatch.delenv("II_MODEL_PROFILE")
    monkeypatch.setattr(Search, "model", "cpu-model")
    started = []
    monkeypatch.setattr(cli, "worker", lambda backend, queue, once: started.append((queue, once)))
    monkeypatch.setattr(sys, "argv", ["backend", "interactive", "--once"])
    if local:
        cli.main()
        assert started == [("interactive", True)]
    else:
        with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
            cli.main()
        assert not started


@pytest.mark.parametrize("local", [True, False], ids=["pilot", "demo"])
def test_pricing_task_only_runs_for_demo_interactive_worker(monkeypatch, local_models, local):
    if not local:
        monkeypatch.delenv("II_MODEL_PROFILE")
    tasks = []
    class Pool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def submit(self, fn, name, operation, interval):
            tasks.append(name)
            completed = Future()
            completed.set_result(None)
            return completed

    monkeypatch.setattr(cli, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    backend = SimpleNamespace(settings=SimpleNamespace(profile="assembly", poll_interval=1,
                                                        interactive_concurrency=1), model_profile=load_model_profile())
    cli.worker(backend, "interactive", False)
    assert tasks == (["interactive"] if local else ["interactive", "model-pricing"])


def test_local_minilm_requires_both_pinned_files_without_downloading(local_models, monkeypatch, tmp_path):
    payload = b"pinned fixture"
    files = ("tokenizer.json", "onnx/model_qint8_arm64.onnx")
    monkeypatch.setattr(artifacts, "FILES", [(name, sha256(payload).hexdigest()) for name in files])
    monkeypatch.setattr(artifacts, "urlopen", lambda *_args, **_kwargs: pytest.fail("unexpected download"))
    with pytest.raises(ValueError, match="tokenizer.json"):
        artifacts.install(tmp_path)
    (tmp_path / "tokenizer.json").write_bytes(payload)
    model = tmp_path / "model_qint8_arm64.onnx"
    model.write_bytes(b"wrong weights")
    with pytest.raises(ValueError, match="model_qint8_arm64.onnx"):
        artifacts.install(tmp_path)
    assert model.read_bytes() == b"wrong weights"
    model.write_bytes(payload)
    artifacts.install(tmp_path)

    # The demo can still fetch a missing artifact and checks the same pinned hash.
    monkeypatch.delenv("II_MODEL_PROFILE")
    model.unlink()
    monkeypatch.setattr(artifacts, "urlopen", lambda url, timeout: BytesIO(payload))
    artifacts.install(tmp_path)
    assert model.read_bytes() == payload


def test_background_faq_retries_through_selected_profile_without_public_key(local_models, dsn, tmp_path, monkeypatch, caplog):
    from backend.assembly import quality, quality_checks
    from backend.assembly.corpus import seed
    from backend.assembly.quality_seed import seed_quality
    from backend.assembly.transport import UpstreamFailure
    from backend.db import connect

    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    calls = []
    monkeypatch.setenv("II_LOCAL_GEMMA_TOKEN", "private-test-token")
    monkeypatch.setattr(quality_checks.LATENCY_LOG, "handlers", [caplog.handler])
    monkeypatch.setattr(quality_checks.LATENCY_LOG, "propagate", False)
    caplog.set_level("INFO", logger="backend.assembly.latency")

    def complete(_, body, stage, *, timeout):
        calls.append(stage)
        if len(calls) == 1:
            raise UpstreamFailure("closed service unavailable")
        return {"text": '{"outcome":"no_source","reason":"Подходящего правила нет."}'}

    monkeypatch.setattr(quality_checks, "complete", complete)
    with connect(dsn) as conn:
        card = conn.execute(quality.CARD_SELECT, quality.SCOPE).fetchone()
        # Provide a free-form answer so the semantic check actually needs Gemma.
        conn.execute("UPDATE faq_entries SET display_question=%s WHERE id=%s",
                     ("Каков срок ответа по регламенту?", card["id"]))
        monkeypatch.setattr(quality_checks, "cited_quantity", lambda *_: None)
        first = quality.prepare_review(conn, "Каков срок ответа по регламенту?", card["answer"], card["conditions"])
        quality.review_after_mail(conn, card["id"], prepared=first)
        assert first["result"]["unavailable"]
        conn.execute("UPDATE assembly_quality_checks SET retry_after=now()")
    assert quality.recheck_revision(SimpleNamespace(dsn=dsn))
    assert calls == ["faq_review", "faq_review"]
    messages = [r.getMessage() for r in caplog.records if r.name == "backend.assembly.latency"]
    assert len(messages) == 2
    assert all("role=gemma profile=local host=gemma.internal" in line for line in messages)
    assert "state=unavailable" in messages[0] and "state=no_source" in messages[1]
    assert all("private-test-token" not in line and card["answer"] not in line for line in messages)
    with connect(dsn) as conn:
        assert conn.execute("SELECT outcome,retry_after FROM assembly_quality_checks").fetchone() == {
            "outcome": "no_source", "retry_after": None}
