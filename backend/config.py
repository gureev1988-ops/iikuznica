"""Configuration and trusted identity directory for the three application modes."""

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


HACKATHON_QUALITY_EMAIL = "quality@softline.test"
INTEGRATED_QUALITY_EMAIL = "quality@softline.ru"
HACKATHON_DOCUMENT_SOURCES = ("synthetic-v1", "batch")


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(ConfigModel):
    id: str = Field(min_length=1)
    roles: frozenset[Literal["employee", "quality", "ops"]]
    email: str | None = None
    bearer_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dion_id: UUID | None = None
    enabled: bool = True

    def actor(self, channel: str):
        from backend.dialog import Actor

        return Actor(id=self.id, roles=self.roles, email=self.email, channel=channel)


class Settings(ConfigModel):
    database_url: SecretStr
    profile: Literal["hackathon", "integrated"] = "hackathon"
    public_base_url: str = "http://localhost:8000"
    identities: list[Identity] = Field(default_factory=list)
    engine: str = "fixture-alpha"
    integrations: dict = Field(default_factory=dict)
    max_pending: int = Field(default=32, ge=10)
    interactive_concurrency: int = Field(default=8, ge=1)
    background_concurrency: int = Field(default=4, ge=1)
    poll_interval: float = Field(default=0.25, gt=0)
    integration_poll_interval: float = Field(default=10, gt=0)

    @model_validator(mode="after")
    def unique_identities(self):
        for attribute in ("id", "bearer_sha256", "dion_id"):
            values = [value for item in self.identities if (value := getattr(item, attribute))]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate identity {attribute}")
        if sum(self._quality_email_matches(item.email) for item in self.identities if item.enabled) > 1:
            raise ValueError("The quality mailbox must identify only one enabled account")
        return self

    @property
    def dsn(self):
        return self.database_url.get_secret_value()

    @property
    def hackathon(self) -> bool:
        return self.profile == "hackathon"

    def _quality_email_matches(self, email: str | None) -> bool:
        expected = HACKATHON_QUALITY_EMAIL if self.hackathon else INTEGRATED_QUALITY_EMAIL
        return bool(email and email.casefold() == expected)

    @property
    def quality_admin(self) -> Identity | None:
        return next((item for item in self.identities if (
            item.enabled and "quality" in item.roles and self._quality_email_matches(item.email)
        )), None)

    def is_quality(self, actor) -> bool:
        identity = self.quality_admin
        return (
            identity is not None and actor is not None and actor.id == identity.id
            and "quality" in actor.roles and self._quality_email_matches(actor.email)
        )

    def by_dion_id(self, external_id):
        for item in self.identities:
            if item.enabled and item.dion_id and str(item.dion_id) == str(external_id):
                return item.actor("dion")
        return None

    def by_actor_id(self, actor_id):
        return next((item for item in self.identities if item.enabled and item.id == actor_id), None)

    def authenticate_token(self, token: str | None):
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for item in self.identities:
                if item.enabled and item.bearer_sha256 and hmac.compare_digest(item.bearer_sha256, digest):
                    return item.actor("web")
        return None

    def actor_dependency(self):
        bearer = HTTPBearer(auto_error=False)

        def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
            actor = self.authenticate_token(credentials.credentials if credentials else None)
            if actor is not None:
                return actor
            raise HTTPException(401, "A valid bearer token is required", headers={"WWW-Authenticate": "Bearer"})

        return authenticate

    @classmethod
    def load(cls):
        path = os.environ.get("II_CONFIG")
        data = json.loads(Path(path).read_text()) if path else {}
        if value := os.environ.get("DATABASE_URL"):
            data["database_url"] = value
        return cls.model_validate(data)
