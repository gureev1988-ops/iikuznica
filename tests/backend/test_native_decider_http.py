from importlib import import_module
from unittest.mock import Mock

from fastapi.testclient import TestClient

from eval.decider.policy import QUESTIONS

server = import_module("deploy.local-inference.decider.server")


def test_native_decisions_use_checkpoint_readouts_and_usage(monkeypatch):
    monkeypatch.setenv("II_DECIDER_API_KEY", "decider-only")
    model = Mock()
    model.decide.return_value = {
        "answers": {
            "decision": {"type": "choice", "choice": "send", "confidence": .54,
                         "probabilities": {"send": .77, "hold": .23}},
            "grounded": {"type": "noul", "noul": .83},
        },
        "usage": {"input_tokens": 243, "output_tokens": 0, "sequences": 2},
    }
    payload = {"model": server.MODEL_ID, "state": "Проверить основание", "questions": {
        "decision": {"type": "choice", "instructions": "Select one", "criteria": {"send": "send", "hold": "hold"}},
        "grounded": QUESTIONS["grounded"],
    }}
    with TestClient(server.create_app(loader=lambda: model)) as client:
        assert client.get("/health").json() == {"model": server.MODEL_ID, "ready": True}
        assert client.post("/v1/decisions", json=payload).status_code == 401
        headers = {"Authorization": "Bearer decider-only"}
        assert client.post("/v1/decisions", json={**payload, "model": "other"}, headers=headers).status_code == 404
        model.decide.assert_not_called()
        result = client.post("/v1/decisions", json=payload, headers=headers)
        assert result.status_code == 200
        assert result.json() == {**model.decide.return_value, "model": server.MODEL_ID}
        model.decide.assert_called_once_with({"state": payload["state"], "questions": payload["questions"]})


def test_native_validation_does_not_publish_a_decision(monkeypatch):
    monkeypatch.delenv("II_DECIDER_API_KEY", raising=False)
    model = Mock()
    model.decide.side_effect = ValueError("native input error")
    with TestClient(server.create_app(loader=lambda: model)) as client:
        payload = {"model": server.MODEL_ID, "state": "text", "questions": QUESTIONS}
        assert client.post("/v1/decisions", json=payload).status_code == 422
        model.decide.assert_called_once()
        assert client.post("/v1/decisions", json={**payload, "images": ["file"]}).status_code == 422
        model.decide.assert_called_once()


def test_loader_requires_offline_checkpoint_and_uses_official_api(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    import pytest

    native = Mock()
    monkeypatch.setitem(sys.modules, "pplx_decider", SimpleNamespace(Decider=native))
    monkeypatch.setattr(server, "CHECKPOINT", tmp_path)
    with pytest.raises(RuntimeError, match="Missing native decision checkpoint"):
        server.load_decider()
    native.from_pretrained.assert_not_called()
    (tmp_path / "joint_config.json").write_text("{}")
    assert server.load_decider() is native.from_pretrained.return_value
    native.from_pretrained.assert_called_once_with(tmp_path, device="cuda", locate=False)
