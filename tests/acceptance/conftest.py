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


@pytest.fixture
def live_http(deployment, tmp_path):
    """Start the factory in a fresh Uvicorn process on an inherited loopback socket."""
    import socket
    import subprocess
    import sys
    import time
    import httpx

    with socket.socket() as bound, (tmp_path / "uvicorn.log").open("w+") as logs:
        bound.bind(("127.0.0.1", 0))
        bound.listen()
        url = f"http://127.0.0.1:{bound.getsockname()[1]}"
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.app:create_app", "--factory", "--fd", str(bound.fileno())],
            pass_fds=[bound.fileno()], stdout=logs, stderr=logs,
        )
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=.4) as http:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and server.poll() is None:
                    try:
                        if http.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    logs.seek(0)
                    pytest.fail("Uvicorn startup failed: " + logs.read()[-2400:])
            yield url
        finally:
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
