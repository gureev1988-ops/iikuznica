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

from deploy.qa.release_smoke import answered_estimate, estimate_question, estimate_scope

playwright = pytest.importorskip("playwright.sync_api")
URL = os.getenv("II_BROWSER_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Set II_BROWSER_URL for the live assembled stand")
DIRECT = "На инициации проекта дирекции ИБ, если все входные данные уже переданы, кто выделяет руководителя проекта и какой у него срок?"
AMBIGUOUS = "За сколько мне назначат руководителя проекта?"


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


def reply(page, request, text):
    search = page.get_by_role("searchbox", name="Поиск по письмам")
    subject = f"Обращение ОК{request['case_number']}"
    search.fill(subject)
    row = page.locator(f'.mail-row[data-mail-id="{request["mail_request_id"]}"]')
    deadline = monotonic() + 40
    while not row.count() and monotonic() < deadline:
        page.get_by_role("button", name="Обновить входящие").click()
        page.wait_for_timeout(350)
    playwright.expect(row.locator(".mail-row-subject")).to_have_text(subject)
    row.click()
    item = page.locator(".mail-reader")
    playwright.expect(item.get_by_role("heading", name=subject, exact=True)).to_be_visible()
    item.get_by_role("button", name="Ответить", exact=True).click()
    item.get_by_role("textbox", name="Текст ответа").fill(text)
    with page.expect_response(lambda r: r.url.endswith("/v1/browser/mail/reply") and r.request.method == "POST") as event:
        item.get_by_role("button", name="Отправить ответ").click()
    assert event.value.status == 202, event.value.text()
    playwright.expect(page.locator("#quality-status")).to_have_text("Ответ отправлен", timeout=55_000)
    return event.value.json()["command_id"]


def ask_new_estimate(page, work):
    for _ in range(3):
        scope = estimate_scope(work)
        turn = answer(page, estimate_question(scope), new=True)
        if turn["offers"]:
            return scope, turn
        assert answered_estimate(turn, scope), turn
        page.wait_for_timeout(1000)
    pytest.fail("Could not open a fresh estimate")


def request_mail(page, work, *, decline=False):
    scope, _ = ask_new_estimate(page, work)
    if decline:
        answer(page, "Нет, не отправляй")
        assert not page.request.get(URL + "/v1/browser/correspondence").json()
        scope, _ = ask_new_estimate(page, work)
    return scope, answer(page, "Да, передай")["result"]


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


def test_04_late_reply_after_closed_tab(guest):
    employee, judge = guest(), guest()
    scope, request = request_mail(employee, "Обновление системы контроля доступа", decline=True)
    context = employee.context
    employee.close()
    login(judge)
    command = reply(judge, request, f"Для {scope} на пресейле ИБ согласован индивидуальный порог маржинальности — 18 процентов.")
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

    # Check this same reply from an independent browser before testing the
    # directorate boundary. It must also work when this one test runs alone.
    own_link = employee.locator("#dion-messages").get_by_text("18 процентов", exact=False).locator("a").first
    expected_faq = own_link.get_attribute("href")
    assert expected_faq
    second = guest()
    turn = answer(second, f"Какую минимальную маржу отдельно согласовали на пресейле информационной безопасности для {scope}?")
    assert turn["result"] and "18 процентов" in turn["result"]["text"] and not turn["offers"], turn
    assert any(ref["kind"] == "faq" and ref["url"] == expected_faq for ref in turn["result"]["sources"])
    source(second, turn)
    other = answer(guest(), f"Какова минимальная маржа для {scope} на пресейле в технической дирекции?")
    assert other["result"] and "18 процентов" not in other["result"]["text"], other


def test_06_seven_profiles_reverse_replies(guest):
    pages = [guest() for _ in range(7)]
    works = (
        "Монтаж серверного оборудования", "Настройка резервного копирования",
        "Миграция корпоративной почты", "Аудит сетевой инфраструктуры",
        "Сопровождение рабочих мест", "Внедрение электронной подписи",
        "Обновление системы мониторинга",
    )
    scopes = [estimate_scope(work) for work in works]
    turns = [submit(page, estimate_question(scope)) for page, scope in zip(pages, scopes)]
    requests = []
    for i, (page, scope, data) in enumerate(zip(pages, scopes, turns)):
        playwright.expect(page.locator("#dion-messages .dion-own").get_by_text(scope, exact=False)).to_be_visible()
        for other in set(scopes) - {scope}:
            assert other not in page.locator("#dion-messages").text_content()
        if i < 2:
            turn = result(page, data)
            if not turn["offers"]:
                assert answered_estimate(turn, scope), turn
                scopes[i], _ = ask_new_estimate(page, works[i])
            requests.append(answer(page, "Да, передай")["result"])
    for judge in pages[5:]:
        login(judge)
    reply(pages[6], requests[1], f"Для {scopes[1]} порог составляет 21 процент.")
    reply(pages[5], requests[0], f"Для {scopes[0]} порог составляет 19 процентов.")
    for i, text in enumerate(("19 процентов", "21 процент")):
        playwright.expect(pages[i].locator("#dion-messages").get_by_text(text, exact=False)).to_be_visible(timeout=45_000)
        assert ("21 процент" if i == 0 else "19 процентов") not in pages[i].locator("#dion-messages").text_content()
    with pages[5].expect_response(lambda r: r.url.endswith("/v1/browser/mail/logout") and r.ok):
        pages[5].get_by_role("button", name="Выйти из режима эксперта").click()
    assert pages[5].request.get(URL + "/v1/browser/mail/inbox").status == 401
    pages[5].get_by_role("button", name="Демо чат DION", exact=True).click()
    open_chat(pages[5])
    playwright.expect(pages[5].locator("#dion-messages")).to_contain_text(scopes[5])


def test_07_out_of_scope_and_external_document(guest):
    page = guest()
    casual = answer(page, "Что приготовить из баклажанов на ужин?")
    assert casual["result"]["action"] == "out_of_scope" and not casual["offers"], casual
    ask_new_estimate(page, "Защита почтового шлюза")


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
    subprocess.run([*compose, "stop", "smtp"], check=True)
    try:
        scope, request = request_mail(employee, "Поддержка резервного центра")
        playwright.expect(employee.locator(".mail-state")).to_contain_text("очереди")
        login(judge)
        assert not judge.locator(f'.mail-row[data-mail-id="{request["mail_request_id"]}"]').count()
        assert answer(guest(), DIRECT)["result"]["sources"]
    finally:
        subprocess.run([*compose, "start", "smtp"], check=True)
    reply(judge, request, f"Для {scope} порог составляет 22 процента.")
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
    page.get_by_role("searchbox", name="Поиск по письмам").fill("Письмо из антарктической обсерватории")
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


@pytest.mark.parametrize("width", [1440, 390])
def test_tech_pilot_navigation_and_approved_text(guest, width):
    page = guest()
    page.set_viewport_size({"width": width, "height": 880})
    tech_tab = page.get_by_role("button", name="Тех.раздел", exact=True)
    tech_tab.click()
    menu = page.locator(".tech-menu a")
    sections = page.locator(".tech-block")
    ids = ["tech-pilot", "tech-routing", "tech-faq", "tech-integrations", "tech-models", "tech-scale", "tech-architecture"]
    assert menu.evaluate_all("(links) => links.map(link => link.hash.slice(1))") == ids
    assert sections.evaluate_all("(blocks) => blocks.map(block => block.id)") == ids
    playwright.expect(menu.first).to_have_text("План пилота")
    playwright.expect(menu.first).to_be_in_viewport()
    title = page.locator("#tech-pilot-title")
    playwright.expect(title).to_be_in_viewport()

    # Compare every heading and list item, including the local-model section, with the approved document.
    plan = (Path(__file__).resolve().parents[2] / "docs/pilot-softline.md").read_text(encoding="utf-8")
    lines = [re.sub(r"^(?:#+|-) ", "", line).replace("**", "") for line in plan.splitlines() if line.strip()]
    assert page.locator("#tech-pilot h3, #tech-pilot h4, #tech-pilot li").all_text_contents() == lines
    roles = re.findall(r"^- \*\*(.*?)\*\*", plan, re.MULTILINE)
    assert page.locator("#tech-pilot li > strong:first-child").all_text_contents() == roles
    playwright.expect(page.locator(".tech-pilot-risk")).to_contain_text("Главный риск")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    menu.last.click()
    playwright.expect(page.locator("#tech-architecture-title")).to_be_in_viewport()
    menu.first.click()
    playwright.expect(page).to_have_url(re.compile(r"#tech-pilot$"))
    playwright.expect(title).to_be_in_viewport()
    menu.last.click()
    page.get_by_role("button", name="Демо чат DION", exact=True).click()
    tech_tab.click()
    playwright.expect(menu.first).to_be_in_viewport()
    playwright.expect(title).to_be_in_viewport()
    page.reload()
    playwright.expect(title).to_be_in_viewport()


def test_quality_card_empty_states(guest):
    page = guest()
    cards = []
    page.route("**/v1/browser/quality/cards", lambda route: route.fulfill(json=cards))
    page.get_by_role("button", name="Отдел качества", exact=True).click()
    playwright.expect(page.get_by_text("Разъяснений пока нет.", exact=True)).to_be_visible()

    cards.append({"id": str(uuid4()), "question": "Тестовое разъяснение", "status": "active", "uses": 0})
    page.get_by_role("button", name="Разъяснения отдела", exact=True).click()
    playwright.expect(page.locator(".faq-row-button")).to_contain_text("Тестовое разъяснение")
    page.locator("#faq-filter").select_option("withdrawn")
    playwright.expect(page.get_by_text("Карточек с таким статусом нет.", exact=True)).to_be_visible()
    page.locator("#faq-filter").select_option("all")
    playwright.expect(page.locator(".faq-row-button")).to_be_visible()


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
    playwright.expect(page.locator("#tech-pilot-title")).to_be_in_viewport()
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


def test_first_send_waits_for_session_and_history(guest):
    page = guest()
    message = page.get_by_role("textbox", name="Сообщение")
    send = page.get_by_role("button", name="Отправить", exact=True)
    text = "В какой срок в Дирекции ИБ назначают РП при инициации?"
    playwright.expect(send).to_be_enabled()
    message.fill(text)
    held = {}
    commands = []

    def pause_history_or_send(route):
        if route.request.method == "POST":
            commands.append(route.request.post_data_json)
            held["send"] = route
        elif "history" not in held:
            held["history"] = route
        else:
            route.continue_()

    page.route("**/v1/browser/session", lambda route: held.update(session=route))
    page.route("**/v1/conversations/*/messages", pause_history_or_send)
    with page.expect_request(lambda req: req.url.endswith("/v1/browser/session")):
        page.reload(wait_until="commit")
    playwright.expect(message).to_be_disabled()
    playwright.expect(send).to_be_disabled()
    page.keyboard.press("Enter")
    with page.expect_request(lambda req: req.url.endswith("/messages") and req.method == "GET"):
        held["session"].continue_()
    playwright.expect(message).to_be_disabled()
    playwright.expect(send).to_be_disabled()
    playwright.expect(message).to_have_value(text)
    page.keyboard.press("Enter")
    assert not commands
    held["history"].continue_()
    playwright.expect(message).to_be_editable()
    playwright.expect(send).to_be_enabled()
    playwright.expect(message).to_have_value(text)
    with page.expect_request(lambda req: req.url.endswith("/messages") and req.method == "POST"):
        message.press("Enter")
    # Key repeat while admission is pending must neither add a newline nor enqueue a second command.
    message.press("Enter")
    playwright.expect(message).to_have_value(text)
    assert len(commands) == 1
    with page.expect_response(lambda res: res.url.endswith("/messages") and res.request.method == "POST") as sent:
        held["send"].continue_()
    assert sent.value.status == 202
    playwright.expect(message).to_have_value("")
    conversation = sent.value.json()["conversation_id"]
    rows = page.request.get(URL + f"/v1/conversations/{conversation}/messages").json()["messages"]
    assert [row["client_message_id"] for row in rows if row["role"] == "user"] == [commands[0]["client_message_id"]]
