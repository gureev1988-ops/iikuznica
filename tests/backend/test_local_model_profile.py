import pytest

from backend.assembly.check_models import main as check_models
from backend.assembly.model_profile import ModelConfigurationError, ROLES, load_model_profile


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("II_MODEL_PROFILE", "local")
    for role in ROLES:
        prefix = f"II_LOCAL_{role.upper()}"
        monkeypatch.setenv(prefix + "_URL", f"https://{role}.example.test:443/inference")
        monkeypatch.setenv(prefix + "_MODEL", f"{role}-v1")
        monkeypatch.delenv(prefix + "_TOKEN", raising=False)


def test_openrouter_remains_default(monkeypatch):
    monkeypatch.delenv("II_MODEL_PROFILE", raising=False)
    assert not load_model_profile().is_local
    monkeypatch.setenv("II_MODEL_PROFILE", "locall")
    with pytest.raises(ModelConfigurationError, match="II_MODEL_PROFILE"):
        load_model_profile()


@pytest.mark.parametrize("url", ["", "https://", "https://user:key@example.test/path",
                                 "https://example.test/path?secret=1", "https://example.test/path#part",
                                 "https://example.test:65536/path", "https://example.test/\npath",
                                 "https://api.openrouter.ai/api/alpha/decisions", "file:///models/decider"])
def test_invalid_local_endpoint_fails_before_use(local, monkeypatch, url):
    monkeypatch.setenv("II_LOCAL_DECIDER_URL", url)
    with pytest.raises(ModelConfigurationError, match="II_LOCAL_DECIDER_URL"):
        load_model_profile()


def test_checks_complete_map_and_keeps_tokens_out_of_output(local, monkeypatch, capsys):
    token = "opaque-synthetic-test-token"
    monkeypatch.setenv("II_LOCAL_QWEN_TOKEN", token)
    profile = load_model_profile()
    assert profile.target("qwen").token == token
    assert profile.target("gemma").token is None
    assert token not in repr(profile.target("qwen"))
    monkeypatch.setattr("sys.argv", ["check_models", "--config-only"])
    assert check_models() == 0
    output = capsys.readouterr().out
    assert all(role + ".example.test" in output for role in ROLES)
    assert token not in output
    monkeypatch.delenv("II_LOCAL_QWEN_MODEL")
    with pytest.raises(ModelConfigurationError, match="II_LOCAL_QWEN_MODEL"):
        load_model_profile()
