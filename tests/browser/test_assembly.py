"""Browser acceptance against a dedicated, freshly seeded Compose project.

Usage: II_BROWSER_URL=https://localhost:4443 python -m pytest -s tests/browser
The optional failure test stops the project's SMTP service temporarily.
"""
import os
from pathlib import Path
import re
import shlex
import subprocess
from time import monotonic
from uuid import uuid4

import pytest

playwright = pytest.importorskip("playwright.sync_api")
URL = os.getenv("II_BROWSER_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Set II_BROWSER_URL for the live assembled stand")
DIRECT = "На инициации проекта дирекции ИБ, если все входные данные уже переданы, кто выделяет руководителя проекта и какой у него срок?"
AMBIGUOUS = "За сколько мне назначат руководителя проекта?"
GAP = "Какой точный процент минимальной маржинальности применяется к проекту пресейла ИБ по внутреннему регламенту контроля маржинальности?"


@pytest.fixture(scope="session")
def browser():
    with playwright.sync_playwright() as p:
        launch = {"executable_path": os.environ["II_CHROMIUM"]} if os.getenv("II_CHROMIUM") else {}
        browser = p.chromium.launch(**launch)
        print("Chromium", browser.version)
        yield browser
        browser.close()


@pytest.fixture
def guest(browser):
    contexts = []

    def new():
        context = browser.new_context(ignore_https_errors=os.getenv("II_BROWSER_LOCAL_CA") == "1")
        contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(70_000)
        open_chat(page)
        return page

    yield new
    for context in contexts:
        context.close()


def open_chat(page):
    with page.expect_response(lambda r: r.url.endswith("/v1/browser/session") and r.ok) as info:
        page.goto(URL)
    session = info.value.json()
    assert session["conversation_id"]
    return session["conversation_id"]


def submit(page, question, *, new=False):
    if new:
        page.get_by_role("button", name="Новая тема").click()
    page.get_by_role("textbox", name="Сообщение").fill(question)
    with page.expect_response(lambda r: "/conversations/" in r.url and r.request.method == "POST") as event:
        page.get_by_role("textbox", name="Сообщение").press("Enter")
    assert event.value.status == 202, event.value.text()
    data = event.value.json()
    print("turn", data["turn_id"])
    return data


def result(page, data):
    token = data["turn_id"]

    def completed():
        response = page.request.get(f"{URL}/v1/turns/{token}")
        assert response.ok
        turn = response.json()
        if turn["status"] in ("completed", "technical_failure"):
            return turn
        return None

    deadline = monotonic() + 115
    while monotonic() < deadline:
        if turn := completed():
            return turn
        page.wait_for_timeout(330)
    pytest.fail(f"Turn timed out: {token}")


def answer(page, question, *, new=False):
    return result(page, submit(page, question, new=new))


def choose(page, label):
    with page.expect_response(lambda r: "/conversations/" in r.url and r.request.method == "POST") as event:
        page.get_by_role("button", name=label).last.click()
    assert event.value.status == 202, event.value.text()
    return result(page, event.value.json())


def source(page, turn, *, point=None):
    assert turn["result"]["sources"]
    for ref in turn["result"]["sources"]:
        response = page.request.get(URL + ref["url"])
        assert response.ok and response.json()["revision_id"] == ref["revision_id"]
        if point is None or response.json()["anchor"] == point:
            page.locator("#dion-messages").locator(f'a[href="{ref["url"]}"]').last.click()
            playwright.expect(page.get_by_role("dialog")).to_be_visible()
            playwright.expect(page.locator("#source-meta")).to_contain_text(str(response.json()["version"]))
            assert ref["quote"] == page.locator("#source-text mark").text_content()
            page.get_by_role("button", name="Закрыть").click()
            return
    pytest.fail(f"Required clause missing: {point}; outcome: {turn['result']}")


def login(page):
    page.get_by_role("button", name="Отдел качества", exact=True).click()
    if page.get_by_role("button", name="Выйти из почты").count():
        return
    env = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
    page.get_by_role("textbox", name="Логин").fill(env["II_JUDGE_LOGIN"])
    page.get_by_label("Пароль").fill(env["II_JUDGE_PASSWORD"])
    page.get_by_role("button", name="Войти в общий ящик").click()
    playwright.expect(page.get_by_role("button", name="Обновить входящие")).to_be_visible()


def opened_mail(page, snippet):
    deadline = monotonic() + 40
    while monotonic() < deadline:
        match = page.locator("details").filter(has_text=snippet).first
        if match.count():
            match.locator("summary").click()
            return match
        page.get_by_role("button", name="Обновить входящие").click()
        page.wait_for_timeout(350)
    pytest.fail("Matching message absent from IMAP inbox")


def reply(page, snippet, text):
    item = opened_mail(page, snippet)
    item.get_by_role("textbox", name="Текст ответа").fill(text)
    with page.expect_response(lambda r: r.url.endswith("/v1/browser/mail/reply") and r.request.method == "POST") as event:
        item.get_by_role("button", name="Reply").click()
    assert event.value.status == 202, event.value.text()
    playwright.expect(item.get_by_text("Ответ отправлен", exact=True)).to_be_visible(timeout=55_000)
    return event.value.json()["command_id"]


def request_mail(page, question, *, decline=False):
    turn = answer(page, question, new=True)
    assert turn["result"] is not None and turn["offers"], turn
    if decline:
        page.get_by_role("button", name="Не отправлять", exact=True).last.click()
        playwright.expect(page.get_by_role("button", name="Отправить письмо", exact=True)).to_have_count(0)
        assert not page.request.get(URL + "/v1/browser/correspondence").json()
        turn = answer(page, question, new=True)
        assert turn["offers"], turn
    page.get_by_role("button", name="Отправить письмо", exact=True).last.click()
    return turn


def test_01_direct_citation(guest):
    page = guest()
    turn = answer(page, DIRECT)
    assert turn["result"]["action"] == "answer", turn
    assert re.search(r"координатор", turn["result"]["text"], re.I)
    assert re.search(r"(?:один|1) рабоч", turn["result"]["text"], re.I)
    source(page, turn, point="2.7")


def test_02_clarification_free_change_and_unknown(guest):
    page = guest()
    turn = answer(page, AMBIGUOUS)
    assert turn["result"]["action"] == "clarify", turn
    playwright.expect(page.get_by_role("textbox", name="Сообщение")).to_have_attribute("placeholder", "Ответьте на уточнение")
    db = result(page, submit(page, "Это проект ДБРЦТ, он уже на инициации."))
    assert db["result"]["action"] == "answer" and "12" in db["result"]["text"], db
    source(page, db, point="7.2")
    ib = answer(page, "Нет, уточню: это дирекция ИБ на инициации, информация уже полная. Какой тогда срок?")
    assert ib["result"]["action"] == "answer", ib
    source(page, ib, point="2.7")
    unclear = answer(page, AMBIGUOUS, new=True)
    assert unclear["result"]["action"] == "clarify"
    unknown = choose(page, "Не знаю")
    assert unknown["result"]["action"] == "answer" and len(unknown["result"]["sources"]) >= 2, unknown


def test_03_compound_procedure_and_permission(guest):
    page = guest()
    turn = answer(page, "Опиши по шагам, как в технической дирекции согласуют договор по результатам конкурса, если проект договора входил в конкурсную документацию.")
    assert turn["result"] and turn["result"]["sources"], turn
    source(page, turn)
    turn = answer(page, "Когда в дирекции ИБ оформлять закрывающие документы? Можно ли оплатить субподрядчику, если есть только его подпись?", new=True)
    assert turn["result"] and turn["result"]["sources"], turn
    if turn["offers"]:
        assert re.search(r"не могу подтвердить|недостаточно|оплат", turn["result"]["text"], re.I)
    else:
        assert re.search(r"нельзя|не может|только после", turn["result"]["text"], re.I)


@pytest.fixture(scope="session")
def run_tag():
    return uuid4().hex[:8]


def test_04_late_reply_after_closed_tab(guest, run_tag):
    employee, judge = guest(), guest()
    question = GAP + f" Код тестового проекта {run_tag}."
    request_mail(employee, question, decline=True)
    context = employee.context
    employee.close()
    login(judge)
    reply(judge, run_tag, "Для указанного тестового проекта минимальный порог маржинальности составляет 18 процентов.")
    employee = context.new_page()
    open_chat(employee)
    playwright.expect(employee.locator("#dion-messages").get_by_text("18 процентов", exact=False)).to_be_visible(timeout=45_000)
    assert "Ответ Отдела качества" not in judge.locator("#dion-messages").text_content()


def test_05_paraphrased_faq_and_scope(guest, run_tag):
    second = guest()
    turn = answer(second, f"Какой нижний предел маржи установлен на пресейле информационной безопасности для тестового проекта {run_tag}?")
    assert turn["result"] and "18 процентов" in turn["result"]["text"] and not turn["offers"], turn
    source(second, turn)
    other = answer(guest(), f"Какова минимальная маржа для проекта {run_tag} на пресейле в технической дирекции?")
    assert other["result"] and "18 процентов" not in other["result"]["text"], other


def test_06_seven_profiles_reverse_replies(guest):
    pages = [guest() for _ in range(7)]
    tags = [uuid4().hex[:8] for _ in pages]
    turns = [submit(page, GAP + f" Код тестового проекта {tag}.") for page, tag in zip(pages, tags)]
    for i, (page, tag, data) in enumerate(zip(pages, tags, turns)):
        playwright.expect(page.locator("#dion-messages").get_by_text(tag, exact=False)).to_be_visible()
        for other in set(tags) - {tag}:
            assert other not in page.locator("#dion-messages").text_content()
        if i < 2:
            assert result(page, data)["offers"]
            page.get_by_role("button", name="Отправить письмо", exact=True).last.click()
    for judge in pages[5:]:
        login(judge)
    reply(pages[6], tags[1], "Для второго тестового проекта порог составляет 21 процент.")
    reply(pages[5], tags[0], "Для первого тестового проекта порог составляет 19 процентов.")
    for i, text in enumerate(("19 процентов", "21 процент")):
        playwright.expect(pages[i].locator("#dion-messages").get_by_text(text, exact=False)).to_be_visible(timeout=45_000)
        assert ("21 процент" if i == 0 else "19 процентов") not in pages[i].locator("#dion-messages").text_content()
    pages[5].get_by_role("button", name="Выйти из почты").click()
    assert pages[5].request.get(URL + "/v1/browser/mail/inbox").status == 401
    pages[5].get_by_role("button", name="Демо DION", exact=True).click()
    open_chat(pages[5])
    playwright.expect(pages[5].locator("#dion-messages")).to_contain_text(tags[5])


def test_07_out_of_scope_and_external_document(guest):
    page = guest()
    casual = answer(page, "Что приготовить из баклажанов на ужин?")
    assert casual["result"]["action"] == "out_of_scope" and not casual["offers"], casual
    gap = answer(page, GAP + f" Для проекта {uuid4().hex[:8]}.", new=True)
    assert gap["result"] is not None and gap["offers"], gap


def test_08_reload_and_idempotent_admit(guest):
    page = guest()
    data = submit(page, DIRECT)
    # Reproduce browser retry after an ambiguous response with the original command body.
    live = page.request.get(f"{URL}/v1/conversations/{data['conversation_id']}/messages").json()
    command = next(m["client_message_id"] for m in live["messages"] if m["turn_id"] == data["turn_id"])
    with page.expect_response(lambda r: r.url.endswith("/v1/browser/session") and r.ok):
        page.reload()
    repeated = page.request.post(f"{URL}/v1/conversations/{data['conversation_id']}/messages",
                                 data={"client_message_id":command, "text":DIRECT, "case_id":None},
                                 headers={"Origin":URL})
    assert repeated.status == 202 and repeated.json() == data
    turn = result(page, data)
    assert turn["result"] and turn["result"]["sources"], turn
    source(page, turn, point="2.7")


@pytest.mark.skipif(not os.getenv("II_BROWSER_COMPOSE"), reason="Set II_BROWSER_COMPOSE for failure injection on dedicated QA Compose")
def test_09_smtp_failure_and_recovery(guest):
    compose = shlex.split(os.environ["II_BROWSER_COMPOSE"])
    employee, judge = guest(), guest()
    tag = uuid4().hex[:8]
    subprocess.run([*compose, "stop", "smtp"], check=True)
    try:
        request_mail(employee, GAP + f" Код тестового проекта {tag}.")
        playwright.expect(employee.locator(".mail-state")).to_contain_text("очереди")
        login(judge)
        assert tag not in judge.locator("#quality-content").text_content()
        assert answer(guest(), DIRECT)["result"]["sources"]
    finally:
        subprocess.run([*compose, "start", "smtp"], check=True)
    reply(judge, tag, "В этом тестовом проекте порог составляет 22 процента.")
    playwright.expect(employee.locator("#dion-messages").get_by_text("22 процента", exact=False)).to_have_count(1, timeout=45_000)
