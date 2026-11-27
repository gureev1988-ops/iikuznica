"""Run from the repository root with python -m backend."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid
import logging
import os
import signal
from threading import Event

from backend.config import Settings


def worker(backend, queue, once):
    stop = Event()
    if once:
        if queue == "background":
            for operation in backend.polling().values():
                operation()
        backend.run_job(queue)
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_args: stop.set())
    workers = (backend.settings.interactive_concurrency if queue == "interactive"
               else backend.settings.background_concurrency)
    tasks = [(queue, lambda: backend.run_job(queue), backend.settings.poll_interval)] * workers
    if queue == "interactive" and backend.settings.profile == "assembly" and not backend.model_profile.is_local:
        from backend.assembly.pricing import refresh
        tasks.append(("model-pricing", refresh, 900))
    if queue == "background":
        tasks.extend((name, operation, backend.settings.integration_poll_interval)
                     for name, operation in backend.polling().items())

    def loop(name, operation, interval):
        while not stop.is_set():
            try:
                busy = operation()
            except Exception as error:
                # Avoid recording message contents, upstream payloads or credentials.
                logging.error("%s iteration failed (%s)", name, type(error).__name__)
                stop.wait(max(1, interval))
            else:
                if not busy:
                    stop.wait(interval)

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(loop, *task) for task in tasks]
        for future in futures:
            future.result()


def simulator_command(backend, args):
    if backend.mail_config.mode != "simulator":
        raise SystemExit("Local mailbox commands require simulator mode")
    actor = backend.settings.authenticate_token(os.getenv("II_QUALITY_TOKEN"))
    if not backend.settings.is_quality(actor):
        raise SystemExit("Local mailbox commands require II_QUALITY_TOKEN for the quality account")
    config = backend.mail_config
    sent = []
    for mid, raw in backend.mailbox.sent():
        original = BytesParser(policy=policy.default).parsebytes(raw)
        if not config.hackathon or str(original["To"]).casefold() == config.recipient:
            sent.append((mid, original))
    if args.mode == "mail-list":
        for mid, message in sent:
            print(mid, message["Subject"])
        return
    for mid, original in sent:
        if mid != args.message_id:
            continue
        message = EmailMessage(policy=policy.SMTP)
        domain = config.recipient.rpartition("@")[2]
        message["Message-ID"] = make_msgid(domain=domain)
        message["From"] = config.recipient
        message["To"] = original["Reply-To"]
        message["Subject"] = "Re: " + str(original["Subject"])
        message["In-Reply-To"] = mid
        message["References"] = f"{original.get('References', '')} {mid}".strip()
        message["Authentication-Results"] = f"{config.authserv_id}; auth=pass smtp.auth={config.recipient}"
        message.set_content(args.text)
        print(backend.mailbox.inject(message.as_bytes()))
        return
    raise SystemExit("Message ID was not found in the local simulator")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    serve = commands.add_parser("api")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    for name in ("interactive", "background"):
        commands.add_parser(name).add_argument("--once", action="store_true")
    commands.add_parser("migrate")
    assembly = commands.add_parser("assembly-init")
    assembly.add_argument("--version", type=int, default=1)
    commands.add_parser("seed-fixtures").add_argument("--version", default=1, type=int, choices=(1, 2))
    commands.add_parser("mail-list")
    reply = commands.add_parser("mail-reply")
    reply.add_argument("message_id")
    reply.add_argument("text")
    args = parser.parse_args()
    settings = Settings.load()
    from backend.db import connect, migrate
    if args.mode == "migrate":
        for name in migrate(settings.dsn):
            print(name)
        return
    if args.mode == "assembly-init":
        from backend.assembly.artifacts import install
        from backend.assembly.model_profile import load_model_profile
        from backend.assembly.corpus import seed
        from backend.assembly.quality_seed import seed_quality
        from backend.assembly.search import load_model
        from pathlib import Path

        profile = load_model_profile()
        migrate(settings.dsn)
        model_dir = Path(os.getenv("II_MODEL_DIR", ".ii-local/models"))
        install(model_dir, profile)
        load_model(model_dir)  # Fail init if the pinned export cannot load on this CPU.
        directory = os.getenv("II_ORIGINALS", ".ii-local/originals")
        seed(settings.dsn, directory, args.version)
        seed_quality(settings.dsn, directory)
        return
    if args.mode == "seed-fixtures":
        from backend.contracts.fixtures import seed_fixture_sources
        with connect(settings.dsn) as conn:
            seed_fixture_sources(conn, active_version=args.version, hackathon=settings.hackathon)
        return
    from backend.app import Backend, create_app
    services = Backend(settings)
    try:
        if settings.profile == "assembly" and args.mode in {"api", "interactive", "background"}:
            latency = logging.getLogger("backend.assembly.latency")
            latency.setLevel(logging.INFO)
            if not latency.handlers:
                latency.addHandler(logging.StreamHandler())
            latency.propagate = False
        if args.mode == "api":
            import uvicorn
            uvicorn.run(create_app(services=services), host=args.host, port=args.port)
        elif args.mode in {"interactive", "background"}:
            if args.mode == "interactive" and settings.profile == "assembly":
                if not services.model_profile.is_local and not os.getenv("OPENROUTER_API_KEY"):
                    raise SystemExit("OPENROUTER_API_KEY is required for the assembly worker")
                _ = services.engine.search.model
            worker(services, args.mode, args.once)
        else:
            simulator_command(services, args)
    finally:
        services.close()


if __name__ == "__main__":
    main()
