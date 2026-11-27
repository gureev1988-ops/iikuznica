from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
import pytest

from backend.assembly.web import Assembly
from backend.config import Settings


def test_explicit_browser_origins_must_match_proxy_host():
    canonical = "https://docs.example.test"
    legacy = "https://192.0.2.10:8443"
    other = "https://other.example.test"
    settings = Settings(database_url="postgresql://unused", public_base_url=canonical)
    config = dict.fromkeys(("II_JUDGE_LOGIN", "II_JUDGE_PASSWORD", "II_BOT_PASSWORD",
                            "II_QUALITY_PASSWORD"), "test-credentials-only")
    assembly = Assembly(settings, credentials=config)
    app = FastAPI()

    @app.post("/")
    def submit(request: Request):
        assembly.check_origin(request)
        return Response(status_code=204)

    client = TestClient(app)
    def post(origin, target, **headers):
        # Caddy forwards HTTP internally and preserves the public Host.
        return client.post("http://upstream/", headers={"Origin": origin,
                           "Host": target.removeprefix("https://"), **headers}).status_code

    assert post(canonical, canonical) == 204
    assert post(legacy, legacy) == 403
    config["II_LEGACY_BROWSER_ORIGIN"] = legacy
    assembly = Assembly(settings, credentials=config)
    assert post(canonical, canonical + ":443") == 204
    assert post(legacy, legacy) == 204
    assert post(legacy + ":443", legacy) == 403
    assert post(other, other) == 403
    assert post(canonical, legacy) == 403
    assert post(legacy, other, **{"X-Forwarded-Host": legacy.removeprefix("https://")}) == 403
    assert post(legacy, legacy, **{"Sec-Fetch-Site": "cross-site"}) == 403
    assert client.post("/", headers={"Host": "docs.example.test"}).status_code == 403
    for invalid in ("http://old.example", "https://old.example/path", legacy + "," + other,
                    "https://u:p@old.example", "https://old.example?x", "https://old.example:70000"):
        with pytest.raises(ValueError):
            Assembly(settings, credentials={**config, "II_LEGACY_BROWSER_ORIGIN": invalid})
