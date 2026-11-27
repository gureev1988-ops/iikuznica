import {actions, button, byId, empty, node, time} from "./quality-ui.js";

const base = "/v1/browser/quality/threads";
const addresses = {
  "bot@assembly.test": ["Регламент бот", "РБ", "bot"],
  "quality@assembly.test": ["Отдел качества", "ОК", "quality"],
};
function person(raw) {
  const address = String(raw || "");
  if (/^guest-[a-f0-9]+@assembly\.test$/i.test(address)) return ["Сотрудник", "С", "employee"];
  return addresses[address] || [address, address.slice(0, 1).toUpperCase(), "other"];
}
const addressList = (value) => Array.isArray(value) ? value.join(", ") : (value || "—");
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

export function renderMessage(message, expanded) {
  const [name, initials, tone] = person(message.sender);
  const badge = node("span", "mail-avatar", initials);
  badge.dataset.tone = tone;
  badge.setAttribute("aria-hidden", "true");
  const body = node("div", "mail-message-body", node("div", "mail-addresses",
    node("p", "", node("strong", "", "От "), addressList(message.sender)),
    node("p", "", node("strong", "", "Кому "), addressList(message.to)),
    node("p", "", node("strong", "", "Копия "), addressList(message.cc))),
    node("p", "quality-copy", message.text || ""));
  const stateText = {interpreting: "Ожидает разбора", review: "Требуется разбор"}[message.processing];
  const details = node("details", "mail-message",
    node("summary", "", badge, node("span", "mail-author", node("strong", "", name),
      node("span", "mail-message-snippet", (message.text || "").replace(/\s+/g, " "))),
    node("span", "mail-message-tools", ...(stateText ? [node("span", "note", stateText)] : []), time(message.at))), body);
  details.open = expanded;
  return node("li", "", details);
}

export function createMail({api, requireExpert, run, status}) {
  let selected;
  let openReader = false;
  let draft;
  let query = "";
  let searchTimer;
  let listVersion = 0;
  let threadVersion = 0;
  let screen;

  function selectThread(id) {
    selected = id;
    openReader = true;
    query = "";
    byId("mail-search").value = "";
    resetDraft();
  }
  function resetDraft() {
    draft = null;
    clearTimeout(searchTimer);
    listVersion++;
    threadVersion++;
  }
  function rows(threads) {
    if (!threads.length) return [empty(query ? "Письма не найдены." : "Пока нет писем.")];
    return threads.map((thread) => {
      const ctl = button("", () => {
        if (selected !== thread.id) resetDraft();
        selected = thread.id;
        openReader = true;
        loadThread(screen);
      }, "mail-row");
      ctl.dataset.mailId = thread.id;
      ctl.setAttribute("aria-current", String(thread.id === selected));
      ctl.append(node("span", "mail-row-meta", node("span", "mail-sender", person(thread.sender)[0]),
        time(thread.updated_at)), node("strong", "mail-row-subject", thread.subject),
      node("span", "mail-preview", (thread.preview || "").replace(/\s+/g, " ")));
      return ctl;
    });
  }
  function composer(thread, reader) {
    const textarea = node("textarea");
    textarea.rows = 5;
    textarea.maxLength = 6000;
    textarea.placeholder = "Напиши разъяснение";
    textarea.setAttribute("aria-label", "Текст ответа");
    textarea.value = draft?.text || "";
    const editing = draft;
    textarea.oninput = () => { editing.text = textarea.value; };
    const outgoing = thread.messages.find((message) => message.message_id === thread.reply_message_id);
    const replyTo = outgoing?.sender || "bot@assembly.test";
    const holder = node("div", "mail-composer", node("div", "mail-composer-head",
      node("strong", "", "Ответить на письмо"),
      node("p", "", "Кому " + replyTo + " · Копия " + addressList(outgoing?.cc))), textarea);
    const feedback = node("p", "note");
    feedback.setAttribute("role", "status");
    const send = button("Отправить ответ", (ctl) => run(ctl, async () => {
      const text = textarea.value.trim();
      if (!text) { feedback.textContent = "Введите текст ответа."; textarea.focus(); return; }
      const submission = draft;
      // Retain the client command after a network error so retry cannot send a second letter.
      if (submission.payload?.text !== text) {
        submission.payload = {message_id: thread.reply_message_id, text, command_id: crypto.randomUUID()};
      }
      textarea.disabled = true;
      cancel.disabled = true;
      try {
        const accepted = await api("/v1/browser/mail/reply", submission.payload);
        feedback.textContent = "Ответ поставлен в очередь";
        const started = Date.now();
        while (Date.now() - started < 60_000) {
          const result = await api("/v1/browser/mail/reply/" + accepted.command_id);
          if (result.state === "sent") {
            feedback.textContent = "Ответ отправлен";
            if (draft === submission) {
              draft = null;
              if (reader.isConnected) {
                holder.replaceChildren(feedback);
                status("Ответ отправлен");
                await list(screen, false);
                // SMTP acceptance precedes the separate IMAP worker and classification.
                // Keep the open thread current when the incoming reply becomes visible.
                await waitForReceipt(thread, reader, result.message_id);
              }
            }
            return;
          }
          if (["failed", "uncertain"].includes(result.state)) {
            feedback.textContent = "Доставка ответа не подтверждена.";
            return;
          }
          if (draft !== submission || !reader.isConnected) return;
          await sleep(800);
        }
        feedback.textContent = "Отправка ещё выполняется. Проверьте состояние повторно.";
      } finally {
        textarea.disabled = false;
        cancel.disabled = false;
      }
    }, (message) => { feedback.textContent = message; }), "button primary");
    const cancel = button("Отмена", () => { draft = null; renderThread(thread, reader); });
    holder.append(node("div", "mail-reply-actions",
      node("p", "note", "Итоговое разъяснение появится в общем списке разъяснений стенда."),
      actions(send, cancel)), feedback);
    return holder;
  }
  async function waitForReceipt(thread, reader, messageId) {
    const ticket = threadVersion;
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline && ticket === threadVersion && reader.isConnected
           && !byId("quality-desk").hidden && selected === thread.id) {
      await sleep(2000);
      if (ticket !== threadVersion || !reader.isConnected || byId("quality-desk").hidden) return;
      const updated = await api(base + "/" + encodeURIComponent(thread.id));
      if (ticket !== threadVersion || !reader.isConnected) return;
      const received = updated.messages.find((item) => item.message_id === messageId);
      if (received?.processing === "interpreting") continue;
      if (received || (!messageId && updated.messages.length > thread.messages.length) || !updated.open) {
        renderThread(updated, reader);
        await list(screen, false);
        return;
      }
    }
  }
  function renderThread(thread, reader) {
    const back = button("К письмам", () => {
      openReader = false;
      screen.root.dataset.reading = "false";
      screen.list.querySelector('[aria-current="true"]')?.focus();
    }, "button mail-mobile-back");
    const heading = node("header", "mail-reader-top", back, node("h3", "", thread.subject));
    if (thread.service) heading.append(node("p", "note", "Служебное письмо · итоги проверки"));
    const messages = node("ol", "mail-thread",
      ...thread.messages.map((message, i) => renderMessage(message, i === thread.messages.length - 1)));
    messages.setAttribute("aria-label", "Сообщения письма");
    reader.replaceChildren(heading, messages);
    if (!thread.open || thread.service || !thread.reply_message_id) return;
    if (draft?.thread === thread.id) reader.append(composer(thread, reader));
    else reader.append(node("div", "mail-thread-actions", actions(button("Ответить", () => requireExpert(() => {
      draft = {thread: thread.id, text: ""};
      renderThread(thread, reader);
      reader.querySelector("textarea").focus();
    }), "button primary"))));
  }

  async function loadThread(view) {
    const ticket = ++threadVersion;
    view.root.dataset.reading = String(openReader);
    view.list.querySelectorAll("[data-mail-id]").forEach((ctl) => {
      ctl.setAttribute("aria-current", String(ctl.dataset.mailId === selected));
    });
    if (!selected) { view.reader.replaceChildren(empty("Выберите письмо.")); return; }
    view.reader.replaceChildren(empty("Загрузка письма…"));
    try {
      const thread = await api(base + "/" + encodeURIComponent(selected));
      if (ticket === threadVersion && view === screen) renderThread(thread, view.reader);
    } catch (error) {
      if (ticket === threadVersion && view === screen) view.reader.replaceChildren(empty(error.message));
    }
  }
  async function list(view, refreshReader) {
    const ticket = ++listVersion;
    const threads = await api(base + (query ? "?q=" + encodeURIComponent(query) : ""));
    if (ticket !== listVersion || view !== screen) return;
    if (refreshReader && (!selected || (query && !threads.some((thread) => thread.id === selected)))) {
      selected = threads[0]?.id;
    }
    view.count.textContent = String(threads.length);
    view.list.replaceChildren(...rows(threads));
    if (refreshReader) await loadThread(view);
  }
  function search() {
    query = byId("mail-search").value.trim();
    resetDraft();
    openReader = false;
    searchTimer = setTimeout(() => list(screen, true).catch((error) => status(error.message)), 180);
  }
  async function render() {
    byId("mail-search").value = query;
    const count = node("span", "", "0");
    count.setAttribute("aria-label", "Количество писем");
    count.id = "mail-list-count";
    const result = node("div", "mail-results");
    const reader = node("section", "mail-reader");
    reader.setAttribute("aria-label", "Область чтения");
    const refresh = button("Обновить входящие", (ctl) => run(ctl, () => list(screen, !draft)),
      "button mail-refresh");
    const inbox = node("section", "mail-inbox", node("div", "mail-inbox-heading",
      node("h3", "", "Входящие"), count, refresh), result);
    const root = node("div", "dion-layout mail-layout", inbox, reader);
    root.id = "mail-client";
    root.dataset.reading = String(openReader);
    screen = {root, list: result, count, reader};
    await list(screen, true);
    return root;
  }
  return {render, selectThread, resetDraft, search};
}
