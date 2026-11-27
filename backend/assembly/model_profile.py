"""Deployment-owned model endpoints shared by the API, workers and offline initialization."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


ROLES = ("gemma", "qwen", "decider")


class ModelConfigurationError(ValueError):
    """A configuration error safe to show without revealing credentials."""


@dataclass(frozen=True)
class ModelTarget:
    url: str
    model_id: str
    token: str | None = field(repr=False)


@dataclass(frozen=True)
class ModelProfile:
    name: str
    targets: dict[str, ModelTarget] = field(default_factory=dict, repr=False)

    @property
    def is_local(self):
        return self.name == "local"

    def target(self, role):
        return self.targets[role]


def _endpoint(value, setting):
    try:
        parts = urlsplit(value)
        url = httpx.URL(value)
        host = url.host.rstrip(".").lower()
        valid = (parts.scheme in ("http", "https") and host and parts.path.strip("/")
                 and parts.username is None and not any(c in value for c in "?#\\")
                 and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
                 and parts.port != 0 and "%" not in parts.netloc)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        raise ModelConfigurationError(f"{setting} must be a full HTTP(S) endpoint without credentials, query or fragment")
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        raise ModelConfigurationError(f"{setting} must point to the local model service")
    return value


def load_model_profile():
    name = os.environ.get("II_MODEL_PROFILE", "openrouter")
    if name == "openrouter":
        return ModelProfile(name)
    if name != "local":
        raise ModelConfigurationError("II_MODEL_PROFILE must be openrouter or local")
    targets = {}
    for role in ROLES:
        prefix = f"II_LOCAL_{role.upper()}"
        url = _endpoint(os.environ.get(prefix + "_URL", ""), prefix + "_URL")
        model_id = os.environ.get(prefix + "_MODEL", "")
        if not model_id or any(c.isspace() or not c.isprintable() for c in model_id):
            raise ModelConfigurationError(f"{prefix}_MODEL must contain the served model ID")
        token = os.environ.get(prefix + "_TOKEN") or None
        if token and not all(33 <= ord(c) <= 126 for c in token):
            raise ModelConfigurationError(f"{prefix}_TOKEN must be a valid HTTP bearer token")
        targets[role] = ModelTarget(url, model_id, token)
    return ModelProfile(name, targets)
