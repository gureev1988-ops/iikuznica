"""Export the public OpenAPI contract from the registered FastAPI application."""

import argparse
import json
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "contracts" / "openapi.v1.json"


def openapi_document() -> dict:
    # The full application is composed alongside the routers in the package branches.
    from backend.app import Backend, create_app
    from backend.config import Settings
    from backend.assembly.web import Assembly
    from backend.mailfaq import MailConfig

    backend = Backend(Settings(database_url="postgresql://localhost/unused"), mail_config=MailConfig(mode="disabled"))
    try:
        app = create_app(services=backend)
        cert_profile = backend.settings.model_copy(update={"public_base_url": "https://example.test"})
        placeholders = dict.fromkeys(("II_JUDGE_LOGIN", "II_JUDGE_PASSWORD", "II_BOT_PASSWORD",
                                      "II_QUALITY_PASSWORD"), "schema-fixture-only")
        backend.assembly = Assembly(cert_profile, credentials=placeholders)
        backend.assembly.mount(app, backend)
        return app.openapi()
    finally:
        backend.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    value = json.dumps(openapi_document(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != value:
            raise SystemExit(f"OpenAPI contract is out of date: {args.output}")
    else:
        args.output.write_text(value, encoding="utf-8")


if __name__ == "__main__":
    main()
