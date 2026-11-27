"""Verify the published revision, browser screens and live answers on the assembly stand."""
import argparse
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler, Request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DIRECT = "На инициации проекта дирекции ИБ, если все входные данные уже переданы, кто выделяет руководителя проекта и какой у него срок?"
UNKNOWN = "Я менеджер по продаже решений в проекте ИБ. Не знаю, все ли данные переданы координатору. Через день РП точно назначат?"
NAVIGATION = "Пришлите сам регламент про пресейл ИБ. По моему запросу ресурс-менеджер не ответил уже рабочий день — к кому могу подключиться?"
GAP = "Какой точный процент минимальной маржинальности применяется к проекту пресейла ИБ по внутреннему регламенту контроля маржинальности?"


class CheckFailure(Exception):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailure(message)


def record(**entry):
    print(json.dumps(entry, ensure_ascii=False), flush=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Stand:
    def __init__(self, base, ca):
        self.base = base
        self.client = build_opener(NoRedirect(), HTTPCookieProcessor(CookieJar()),
                                   HTTPSHandler(context=ssl.create_default_context(cafile=ca)))

    def request(self, path, data=None, method=None):
        check(path.startswith("/") and not path.startswith("//"), "Unexpected path from stand")
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
        req = Request(self.base + path, data=body, method=method,
                      headers={"Origin": self.base, "Content-Type": "application/json"})
        with self.client.open(req, timeout=15) as response:
            return response.status, response.headers, response.read()

    def json(self, path, data=None, method=None):
        return json.loads(self.request(path, data, method)[2])

    def session(self):
        session = self.json("/v1/browser/session", {})
        self.conversation = session["conversation_id"]
        self.standard_demo_login = session.get("standard_demo_login") is True

    def answer(self, question, **context):
        start = time.monotonic()
        submission = {"text": question, "client_message_id": str(uuid4()), **context}
        accepted = self.json(f"/v1/conversations/{self.conversation}/messages", submission)
        turn_id = accepted["turn_id"]
        def finished():
            turn = self.json("/v1/turns/" + turn_id)
            check(turn["status"] not in ("technical_failure", "failed"), f"Turn {turn_id} failed")
            return turn if turn["status"] == "completed" else None
        turn = wait_for(finished, 150, f"turn {turn_id}")
        return turn, round(time.monotonic() - start, 2)


def wait_for(read, timeout, description):
    deadline = time.monotonic() + timeout
    # These callbacks only poll existing turns or mail after the write was accepted.
    while True:
        try:
            value = read()
            if value:
                return value
        except HTTPError as exc:
            if exc.code not in (502, 503, 504):
                raise
        if time.monotonic() >= deadline:
            raise CheckFailure(f"Timed out waiting for {description}")
        time.sleep(1)


def exact_revision(stand, sha, seconds):
    deadline = time.monotonic() + seconds
    previous = object()
    while True:
        try:
            status, headers, body = stand.request("/healthz")
            state = headers.get("X-IIKuznica-Revision")
            health = json.loads(body)
            if status == 200 and state == sha and health.get("database") == "ready" and health.get("regulations") == 4:
                record(check="published revision", sha=sha, https=stand.base)
                return
            evidence = state or f"HTTP {status}, no revision"
        except (OSError, ValueError, HTTPError) as exc:
            evidence = str(exc)
        if evidence != previous:
            record(check="waiting for revision", observed=evidence)
            previous = evidence
        if time.monotonic() >= deadline:
            raise CheckFailure(f"Could not verify {sha} at {stand.base}: {evidence}")
        time.sleep(min(10, max(0, deadline - time.monotonic())))


def answer_check(stand, label, question, anchors, words):
    turn, elapsed = stand.answer(question)
    result = turn["result"]
    observed = []
    for ref in result["sources"]:
        clause = stand.json(ref["url"])
        check(ref["quote"] in clause["text"] and ref["revision_id"] == clause["revision_id"],
              f"Source text or revision mismatch for {label}")
        observed.append(clause["anchor"])
    text = result["text"]
    passed = anchors.issubset(observed) and all(re.search(p, text, re.I) for p in words)
    record(check=label, passed=bool(passed), question=question, answer=text,
           action=result["action"], seconds=elapsed, anchors=observed, turn=turn["turn_id"])
    return bool(passed)


def live_answers(guests):
    cases = [
        ("direct; known", DIRECT, {"2.7"}, [r"координатор", r"(?:1|один|одного)\s+рабоч"]),
        ("unknown condition; new wording", UNKNOWN, {"2.2", "2.7"}, [r"неполн|не полн|не хват|не все|уточн"]),
        ("compound; new wording", NAVIGATION, {"7.9"}, [r"линейно\w*\s+руководител"]),
    ]
    results = []
    for client, (label, question, anchors, words) in zip(guests, cases):
        results.append(answer_check(client, label, question, anchors, words))
    check(all(results), "A live answer failed the predeclared content/grounding checks")


def shared_screens(guests):
    ids = {g.conversation for g in guests}
    check(len(ids) == len(guests), "Guest session collision")
    for g in guests:
        check(g.json("/v1/browser/session", {})["conversation_id"] == g.conversation, "Session changed")
    docs = guests[0].json("/v1/browser/documents")
    check(len(docs) >= 4, "Missing seeded regulations")
    for doc in docs:
        path = f"/v1/browser/documents/{doc['document_id']}/revisions/{doc['revision_id']}"
        revision = guests[0].json(path)
        check(revision["fragments"] and revision["revision_id"] == doc["revision_id"], "Incomplete revision")
        status, _, payload = guests[0].request(path + "/original")
        check(status == 200 and payload, "Missing original")
    stats = guests[0].json("/v1/browser/statistics")
    check(stats["period"]["timezone"] == "Europe/Moscow" and "ratings" in stats, "Missing statistics")
    cards = guests[0].json("/v1/browser/quality/cards")
    threads = guests[0].json("/v1/browser/quality/threads?q=" + quote("Обращение ОК1"))
    seed = [thread for thread in threads if thread["subject"] == "Обращение ОК1"]
    check(len(seed) == 1, "Expected exactly one OK1 demo thread")
    check(any(card["request_id"] == seed[0]["id"] and card["uses"] >= 5 for card in cards),
          "Missing FAQ or baseline counter for the OK1 demo thread")
    try:
        guests[1].request("/v1/conversations/" + guests[0].conversation + "/messages")
    except HTTPError as error:
        check(error.code in (403, 404), f"Unexpected status for other own test session: {error.code}")
    else:
        raise CheckFailure("Separate test profiles could read each other's histories")
    record(check="public API", guests=len(ids), regulations=len(docs), public_statistics=True)


def private_roundtrip(guest):
    question = "Что приготовить из баклажанов на ужин?"
    turn, elapsed = guest.answer(question)
    result = turn["result"]
    check(result["action"] == "out_of_scope" and not turn["offers"] and not result["sources"],
          "Unrelated question created an escalation or source issue")
    messages = guest.json(f"/v1/conversations/{guest.conversation}/messages")["messages"]
    check(any(m["text"] == question for m in messages), "Own question missing from recovered chat")
    check(any(m["role"] == "assistant" and m["text"] == result["text"] for m in messages),
          "Own answer missing from recovered chat")
    record(check="own chat round trip", action=result["action"], seconds=elapsed)


def run_browser_tests(base, full):
    test_file = "tests/browser/test_assembly.py"
    checks = ["test_quality_readonly_workspace", "test_dion_draft_guide_and_ime"]
    if full:
        checks.append("test_02_clarification_free_change_and_unknown")
    env = os.environ.copy()
    env["II_BROWSER_URL"] = base
    env.pop("II_BROWSER_LOCAL_CA", None)
    env.pop("II_BROWSER_COMPOSE", None)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "--confcutdir=tests/browser",
                    *(test_file + "::" + case for case in checks)],
                   cwd=ROOT, env=env, check=True)


def live_mail(employee, expert, reader):
    login, password = os.environ.get("II_SMOKE_EXPERT_LOGIN"), os.environ.get("II_SMOKE_EXPERT_PASSWORD")
    if not login and not password and expert.standard_demo_login:
        login = password = "admin"
    check(login and password, "Mail check requires the stand's configured expert credentials")
    check(expert.json("/v1/browser/mail/login", {"login": login, "password": password})["mail"],
          "Expert session unavailable")
    tag = "QA-" + uuid4().hex[:10]
    question = GAP + f" Это только для учебного проекта {tag}."
    gap, _ = employee.answer(question)
    offer = next((o for o in gap["offers"] if o["status"] == "open"), None)
    check(offer, "No escalation offered for the marked gap")
    employee.answer("Да, передай", case_id=gap["case_id"])
    def matching_thread():
        hits = expert.json("/v1/browser/quality/threads?q=" + tag)
        check(len(hits) <= 1, "Duplicate synthetic thread")
        if hits:
            thread = expert.json("/v1/browser/quality/threads/" + hits[0]["id"])
            if thread["reply_message_id"] and thread["open"]:
                return thread
    thread = wait_for(matching_thread, 100, "synthetic QA email")
    check(re.fullmatch(r"Обращение ОК\d+", thread["subject"]), "Missing stable request number")
    original = next(m for m in thread["messages"] if m["message_id"] == thread["reply_message_id"])
    check(original["sender"] == "bot@assembly.test" and original["to"] == "quality@assembly.test"
          and re.fullmatch(r"guest-[a-f0-9]{32}@assembly\.test", original["cc"]),
          "Unexpected sender, recipient or copy address for the test request")
    check(tag in original["text"] and "Да, передай" in original["text"] and all(
        section in original["text"] for section in ("Вопрос от сотрудника", "Почему направлено обращение", "Диалог до отправки")
    ), "Missing request content, consent or mail section")
    command = {"message_id": thread["reply_message_id"], "command_id": str(uuid4()),
               "text": f"Только для учебного проекта {tag} минимальный порог маржинальности составляет 18 процентов."}
    expert.json("/v1/browser/mail/reply", command)
    def delivered_reply():
        messages = employee.json(f"/v1/conversations/{employee.conversation}/messages")["messages"]
        return any(tag in m["text"] and "18 процентов" in m["text"] and m["role"] != "user" for m in messages)
    wait_for(delivered_reply, 150, "expert response in original chat")
    closed = expert.json("/v1/browser/quality/threads/" + thread["id"])
    replies = [message for message in closed["messages"] if
               message["message_id"] == f"<judge-{command['command_id']}@assembly.test>"]
    check(not closed["open"] and len(replies) == 1, "Expected one expert reply in the closed training case")
    check(replies[0]["sender"] == "quality@assembly.test" and replies[0]["to"] == "bot@assembly.test"
          and replies[0]["cc"] == original["cc"], "Unexpected recipient or copy address in expert reply")
    repeated, _ = reader.answer(question)
    check("18" in repeated["result"]["text"] and any(ref["kind"] == "faq" for ref in repeated["result"]["sources"]),
          "Independent guest did not receive the synthetic FAQ")
    check(not expert.json("/v1/browser/mail/logout", {})["mail"], "Logout failed")
    record(check="mail round trip", project=tag, subject=thread["subject"], faq_reused=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--wait", type=int, default=0, help="Seconds to allow for rollout")
    parser.add_argument("--ca", default=os.getenv("SSL_CERT_FILE"))
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--roundtrip", action="store_true", help="Submit one unrelated question in a fresh private chat")
    parser.add_argument("--answers", action="store_true")
    parser.add_argument("--mail", action="store_true", help="Create one scoped synthetic test correspondence")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    dest = urlsplit(base)
    check(dest.scheme == "https" and dest.hostname and not (dest.path or dest.query or dest.fragment or dest.username),
          "Supply the HTTPS origin of the assembly stand")
    check(re.fullmatch(r"[0-9a-f]{40}", args.sha), "Supply the full Git commit SHA")
    guests = [Stand(base, args.ca) for _ in range(7)]
    exact_revision(guests[0], args.sha, args.wait)
    for guest in guests:
        guest.session()
    shared_screens(guests)
    if args.roundtrip:
        private_roundtrip(guests[6])
    if args.browser:
        run_browser_tests(base, args.answers)
    if args.answers:
        live_answers(guests)
    if args.mail:
        live_mail(*guests[3:6])
    exact_revision(guests[0], args.sha, 0)
    record(check="finished", sha=args.sha, time_utc=datetime.now(timezone.utc).isoformat(),
           browser=args.browser, public_roundtrip=args.roundtrip, live_answers=args.answers, mail=args.mail)


if __name__ == "__main__":
    try:
        main()
    except (CheckFailure, HTTPError, URLError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
