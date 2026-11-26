from hashlib import sha256
import json
from uuid import uuid4

import pytest


@pytest.fixture
def deployment(dsn, tmp_path, monkeypatch):
    from backend.config import Settings

    identities = [
        {"id": name, "roles": roles, "email": f"{name}@example.test",
         "bearer_sha256": sha256(f"fixture-{name}".encode()).hexdigest()}
        for name, roles in (("first", ["employee"]), ("second", ["employee"]),
                            ("operator", ["ops"]), ("quality", ["quality"]))
    ]
    config = tmp_path / "settings.json"
    mailbox = tmp_path / "local-mail.sqlite"
    values = dict(identities=identities, max_pending=16, integrations={"batch": []}, poll_interval=0.05)
    config.write_text(json.dumps(values))
    monkeypatch.setenv("II_CONFIG", str(config))
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("MAIL_MODE", "simulator")
    monkeypatch.setenv("II_LOCAL_MAILBOX", str(mailbox))
    return Settings.load(), config, mailbox


def credentials(actor):
    return {"Authorization": f"Bearer fixture-{actor}"}
