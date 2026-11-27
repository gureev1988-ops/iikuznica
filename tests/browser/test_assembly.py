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
            if ref["kind"] == "document":
                page.get_by_role("link", name="Открыть регламент целиком").click()
                playwright.expect(page.locator(".regulation-highlight")).to_contain_text(response.json()["anchor"])
                playwright.expect(page.locator("#regulation-original")).to_be_visible()
                page.locator("#regulation-close").click()
            page.locator("#dion-close-source").click()
            return
    pytest.fail(f"Required clause missing: {point}; outcome: {turn['result']}")


def login(page):
    page.get_by_role("button", name="Отдел качества", exact=True).click()
    if page.locator("#quality-login").get_attribute("aria-label") != "Выйти из режима эксперта":
        env = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
        page.get_by_role("button", name="Войти как эксперт").click()
        page.get_by_role("textbox", name="Логин").fill(env["II_JUDGE_LOGIN"])
        page.get_by_label("Пароль").fill(env["II_JUDGE_PASSWORD"])
        page.get_by_role("dialog").get_by_role("button", name="Войти", exact=True).click()
        playwright.expect(page.get_by_role("button", name="Выйти из режима эксперта")).to_be_visible()
    page.get_by_role("button", name="Входящие письма", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Обновить входящие")).to_be_visible()


def opened_mail(page, snippet):
    search = page.get_by_role("searchbox", name="Поиск по письмам")
    search.fill(snippet)
    mailbox = page.locator(".mail-reader")
    deadline = monotonic() + 40
    while monotonic() < deadline:
        if mailbox.get_by_text(snippet, exact=False).count() and mailbox.get_by_role("button", name="Ответить", exact=True).count():
            mailbox.get_by_role("button", name="Ответить", exact=True).click()
            return mailbox
        page.get_by_role("button", name="Обновить входящие").click()
        page.wait_for_timeout(350)
    pytest.fail("Matching open thread absent from the mailbox")


def reply(page, snippet, text):
    item = opened_mail(page, snippet)
    item.get_by_role("textbox", name="Текст ответа").fill(text)
    with page.expect_response(lambda r: r.url.endswith("/v1/browser/mail/reply") and r.request.method == "POST") as event:
        item.get_by_role("button", name="Отправить ответ").click()
    assert event.value.status == 202, event.value.text()
    playwright.expect(page.locator("#quality-status")).to_have_text("Ответ отправлен", timeout=55_000)
    return event.value.json()["command_id"]


def request_mail(page, question, *, decline=False):
    turn = answer(page, question, new=True)
    assert turn["result"] is not None and turn["offers"], turn
    if decline:
        answer(page, "Нет, не отправляй")
        assert not page.request.get(URL + "/v1/browser/correspondence").json()
        turn = answer(page, question, new=True)
        assert turn["offers"], turn
    answer(page, "Да, передай")
    return turn


def test_01_direct_citation(guest):
    page = guest()
    independent = guest()
    turn = answer(page, DIRECT)
    assert turn["result"]["action"] == "answer", turn
    assert re.search(r"координатор", turn["result"]["text"], re.I)
    assert re.search(r"(?:один|1) рабоч", turn["result"]["text"], re.I)
    playwright.expect(page.locator("#dion-hint-text")).to_contain_text("Откройте процитированный пункт")
    source(page, turn, point="2.7")
    playwright.expect(page.locator("#dion-hint-text")).to_contain_text("составной вопрос")
    assert independent.request.get(URL + "/v1/browser/hint").json()["stage"] == "simple"
    assert independent.request.get(URL + "/v1/browser/last-log").json()["turn_id"] is None
    assert independent.request.get(URL + "/v1/turns/" + turn["turn_id"]).status in (403, 404)
    vote = page.get_by_role("button", name="Помогло", exact=True).last
    vote.click()
    playwright.expect(vote).to_have_attribute("aria-pressed", "true")
    page.reload()
    playwright.expect(vote).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Логи", exact=True).click()
    playwright.expect(page.locator("#trace-id")).not_to_have_text("")
    playwright.expect(page.locator("#events")).to_contain_text("локально")


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
    options = unclear["clarifications"][0]["options"]
    number = next(i for i, option in enumerate(options, 1) if option["text"] == "Не знаю")
    shown = page.locator(f'#dion-messages [data-case-id="{unclear["case_id"]}"]').filter(has_text=f"{number}. Не знаю")
    playwright.expect(shown).to_be_visible()
    playwright.expect(page.get_by_role("textbox", name="Сообщение")).to_have_attribute("placeholder", "Ответьте на уточнение")
    unknown = answer(page, str(number))
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
    command = reply(judge, run_tag, "Для указанного тестового проекта минимальный порог маржинальности составляет 18 процентов.")
    employee = context.new_page()
    open_chat(employee)
    playwright.expect(employee.locator("#dion-messages").get_by_text("18 процентов", exact=False)).to_be_visible(timeout=45_000)
    assert "18 процентов" not in judge.locator("#dion-messages").text_content()
    if command_line := os.getenv("II_BROWSER_COMPOSE"):
        compose = shlex.split(command_line)
        env = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
        variables = dict(os.environ, II_QA_REPLY_ID=command, II_IMAP_OPERATOR_PASSWORD=env["II_IMAP_OPERATOR_PASSWORD"])
        subprocess.run([*compose, "exec", "-T", "-e", "II_QA_REPLY_ID", "-e", "II_IMAP_OPERATOR_PASSWORD",
                        "background", "python", "-"], input=Path("deploy/qa/mail.py").read_bytes(),
                       env=variables, check=True)
        playwright.expect(employee.locator("#dion-messages").get_by_text("18 процентов", exact=False)).to_have_count(1)


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
            answer(page, "Да, передай")
    for judge in pages[5:]:
        login(judge)
    reply(pages[6], tags[1], "Для второго тестового проекта порог составляет 21 процент.")
    reply(pages[5], tags[0], "Для первого тестового проекта порог составляет 19 процентов.")
    for i, text in enumerate(("19 процентов", "21 процент")):
        playwright.expect(pages[i].locator("#dion-messages").get_by_text(text, exact=False)).to_be_visible(timeout=45_000)
        assert ("21 процент" if i == 0 else "19 процентов") not in pages[i].locator("#dion-messages").text_content()
    with pages[5].expect_response(lambda r: r.url.endswith("/v1/browser/mail/logout") and r.ok):
        pages[5].get_by_role("button", name="Выйти из режима эксперта").click()
    assert pages[5].request.get(URL + "/v1/browser/mail/inbox").status == 401
    pages[5].get_by_role("button", name="Демо чат DION", exact=True).click()
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
    if command_line := os.getenv("II_BROWSER_COMPOSE"):
        compose = shlex.split(command_line)
        old_refs = turn["result"]["sources"]
        before = page.request.get(URL + old_refs[0]["url"]).json()
        subprocess.run([*compose, "run", "--rm", "--no-deps", "init", "assembly-init",
                        "--version", str(before["version"] + 1)], check=True)
        assert page.request.get(URL + old_refs[0]["url"]).json() == before
        source(page, turn, point="2.7")
        new_turn = answer(page, DIRECT, new=True)
        assert new_turn["result"] and new_turn["result"]["sources"]
        after = page.request.get(URL + new_turn["result"]["sources"][0]["url"]).json()
        assert after["version"] == before["version"] + 1 and after["revision_id"] != before["revision_id"]
        source(page, new_turn, point="2.7")


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


@pytest.mark.parametrize("width", [1440, 390])
def test_quality_readonly_workspace(guest, width):
    page = guest()
    page.set_viewport_size({"width": width, "height": 880})
    page.get_by_role("button", name="Отдел качества", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Разъяснения", exact=True)).to_be_visible()
    card = page.locator(".faq-row-button").first
    playwright.expect(card).to_contain_text("Использовано")
    card.click()
    page.get_by_role("button", name="Редактировать").click()
    playwright.expect(page.locator("#expert-dialog")).to_be_visible()
    page.get_by_role("dialog").get_by_role("button", name="Закрыть").click()
    page.get_by_role("button", name="Открыть всю переписку").click()
    playwright.expect(page.locator("#faq-thread-messages li").first).to_be_visible()
    page.get_by_role("dialog").get_by_role("button", name="Закрыть").click()
    page.get_by_role("button", name="Входящие письма", exact=True).click()
    playwright.expect(page.locator(".mail-row").first).to_be_visible()
    page.locator(".mail-row").first.click()
    playwright.expect(page.locator(".mail-reader h3")).to_be_visible()
    if width < 600:
        page.get_by_role("button", name="К письмам").click()
    page.get_by_role("searchbox", name="Поиск по письмам").fill(uuid4().hex)
    playwright.expect(page.get_by_text("Письма не найдены.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Источники", exact=True).click()
    page.get_by_text("Регламенты", exact=True).click()
    playwright.expect(page.locator(".source-regulations li").first).to_be_visible()
    page.locator(".source-regulations .quality-link").first.click()
    playwright.expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Статистика").click()
    for title in ("Востребованные регламенты", "Проблемные пункты", "Повторяющиеся вопросы без ответа", "Оценки ответов"):
        playwright.expect(page.get_by_role("heading", name=title)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_dion_draft_guide_and_ime(guest):
    page = guest()
    draft = page.get_by_role("textbox", name="Сообщение")
    playwright.expect(page.locator("#dion-messages article")).to_have_count(0)
    page.get_by_role("button", name="Логи", exact=True).click()
    playwright.expect(page.locator("#trace-status")).to_contain_text("первого вопроса")
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Применить", exact=True).click()
    example = draft.input_value()
    assert example
    draft.press("End")
    draft.press("Shift+Enter")
    draft.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
    playwright.expect(draft).to_have_value(example + "\n")
    page.get_by_role("button", name="Тех.раздел", exact=True).click()
    playwright.expect(page.locator(".tech-block")).to_have_count(6)
    page.get_by_role("link", name="Модели и ресурсы").click()
    playwright.expect(page.locator("#tech-models")).to_be_in_viewport()
    page.get_by_role("button", name="Демо чат DION", exact=True).click()
    playwright.expect(draft).to_have_value(example + "\n")
    playwright.expect(page.locator("#dion-messages article")).to_have_count(0)


def test_dion_guided_examples_create_separate_cases(guest):
    page = guest()
    hint_text = page.locator("#dion-hint-text")
    apply = page.locator("#dion-hint-apply")
    composer = page.get_by_role("textbox", name="Сообщение")

    def apply_and_send():
        apply.click()
        assert composer.input_value()
        with page.expect_response(lambda r: "/conversations/" in r.url and r.request.method == "POST") as sent:
            composer.press("Enter")
        response = sent.value
        assert response.status == 202, response.text()
        return result(page, response.json()), response.request.post_data_json

    playwright.expect(hint_text).to_contain_text("простого вопроса")
    simple, command = apply_and_send()
    assert simple["result"]["action"] == "answer" and command["case_id"] is None
    source(page, simple)

    playwright.expect(hint_text).to_contain_text("составной вопрос")
    compound, command = apply_and_send()
    assert command["case_id"] is None and compound["case_id"] != simple["case_id"]
    assert len(compound["result"]["parts"]) >= 2
    assert all(part["action"] == "answer" and part["sources"] for part in compound["result"]["parts"])

    playwright.expect(hint_text).to_contain_text("без указания дирекции")
    clarify, command = apply_and_send()
    assert command["case_id"] is None and clarify["result"]["action"] == "clarify"
    assert clarify["case_id"] not in {simple["case_id"], compound["case_id"]}
    playwright.expect(hint_text).to_contain_text("Ответьте на уточнение")

    # The selected topic can differ from the question linked by the guide.
    page.locator("#topics button").first.click()
    continuation, command = apply_and_send()
    assert command["case_id"] == clarify["case_id"] == continuation["case_id"]
    assert command["clarification_id"] == clarify["clarifications"][0]["id"]
    assert continuation["result"]["action"] == "answer" and continuation["result"]["sources"]
    # The server skips mail examples that the shared FAQ can already answer.
    assert page.request.get(URL + "/v1/browser/hint").json()["stage"] in {"gap", "done"}
