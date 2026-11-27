const $ = (id) => document.getElementById(id);
const el = (tag, text = "") => {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
};
const button = (label, action) => {
  const node = el("button", label);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
};

let conversation;
let activeCase;
let history;
let mailStates = [];
let mail = false;
let tab = "inbox";
let stream;
let refreshTimer;
let deeperSearchTimer;
let refreshing = false;
let needRefresh = false;
let correspondenceRefresh;
let viewVersion = 0;
const status = (message) => { $("chat-status").textContent = message; };
const mailStatus = (message) => { $("quality-status").textContent = message; };
const SEARCHING = "Ищу и проверяю основания…";
const DEEPER_SEARCH = "Осуществляю более глубокий поиск…";

function updateSearchStatus() {
  clearTimeout(deeperSearchTimer);
  const pending = history.turns.filter((turn) => ["queued", "processing"].includes(turn.status));
  if (!pending.length) {
    if ([SEARCHING, DEEPER_SEARCH].includes($("chat-status").textContent)) status("");
    return;
  }
  const waitingFor = new Set(pending.map((turn) => turn.turn_id));
  const since = history.messages.find((msg) => msg.role === "user" && waitingFor.has(msg.turn_id));
  const elapsed = since ? Date.now() - Date.parse(since.created_at) : 0;
  const delay = 4000 - Math.max(0, elapsed);
  status(delay <= 0 ? DEEPER_SEARCH : SEARCHING);
  if (delay > 0) deeperSearchTimer = setTimeout(() => status(DEEPER_SEARCH), delay);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  };
  const response = await fetch(path, {...options, cache: "no-store"});
  const data = await response.json();
  if (!response.ok) {
    const error = Error(typeof data.detail === "string" ? data.detail : (data.detail?.code || "Ошибка запроса"));
    error.status = response.status;
    throw error;
  }
  return data;
}

function perform(node, operation, report = status) {
  node.disabled = true;
  report("");
  return Promise.resolve().then(operation).catch((error) => report(error.message))
    .finally(() => { node.disabled = false; });
}

async function showSource(ref) {
  const title = $("source-title");
  const text = $("source-text");
  title.textContent = "Источник";
  $("source-meta").textContent = "";
  text.textContent = "Загрузка…";
  if (!$("source").open) $("source").showModal();
  try {
    const source = await api(ref.url);
    title.textContent = source.title;
    $("source-meta").textContent = source.kind === "document"
      ? `Пункт ${source.anchor} · редакция ${source.version ?? source.revision_id}`
      : `Разъяснение Отдела качества · редакция ${source.version ?? source.revision_id}`;
    const start = source.text.indexOf(ref.quote);
    if (ref.quote && start >= 0) {
      text.replaceChildren(source.text.slice(0, start), el("mark", ref.quote), source.text.slice(start + ref.quote.length));
    } else {
      text.textContent = source.text;
    }
  } catch (error) {
    text.textContent = error.message;
  }
}

function sourceLink(ref, label) {
  const link = el("a", label);
  link.href = ref.url;
  link.addEventListener("click", (event) => {
    event.preventDefault();
    showSource(ref);
  });
  return link;
}

function renderText(node, text, refs = []) {
  const mentioned = new Set();
  const split = text.split(/(\[\d+\])/);
  for (const part of split) {
    const index = Number(part.slice(1, -1)) - 1;
    if (/^\[\d+\]$/.test(part) && refs[index]) {
      mentioned.add(index);
      node.append(sourceLink(refs[index], part));
    } else {
      node.append(part);
    }
  }
  refs.forEach((ref, i) => {
    if (!mentioned.has(i)) node.append(" ", sourceLink(ref, `[${i + 1}] ${ref.kind === "faq" ? "Разъяснение" : "Открыть пункт"}`));
  });
}

const labels = {
  waiting_send: "Письмо в очереди", waiting_reply: "Письмо доставлено · ожидаем ответ",
  resolved: "Получен ответ Отдела качества", counter_question: "Отдел качества запросил уточнение",
  partial: "Получен частичный ответ", delivery_failed: "Не удалось подтвердить доставку",
  withdrawn: "Обращение отозвано",
};

function selectCase(value) {
  activeCase = value;
  if (value) localStorage.setItem(`case:${conversation}`, value);
  else localStorage.removeItem(`case:${conversation}`);
  render();
  $("message").focus();
}

function openClarification() {
  return history?.turns.filter((turn) => turn.case_id === activeCase)
    .flatMap((turn) => turn.clarifications).find((action) => action.status === "open");
}

function render() {
  if (!history) return;
  const scroll = $("dion-scroll");
  const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
  const turns = new Map(history.turns.map((turn) => [turn.turn_id, turn]));
  const topics = new Map();
  for (const turn of history.turns) if (!topics.has(turn.case_id)) topics.set(turn.case_id, turn.question);
  const topicNodes = [...topics].map(([id, title]) => {
    const ctl = button(title, () => selectCase(id));
    ctl.title = title;
    if (activeCase === id) ctl.setAttribute("aria-current", "true");
    return ctl;
  });
  $("topics").replaceChildren(...topicNodes);
  const items = [];
  if (!history.messages.length) {
    const message = el("article", "Здравствуйте. Напишите вопрос по регламенту.");
    message.className = "message";
    items.push(message);
  }
  for (const msg of history.messages) {
    const item = el("article");
    item.className = msg.role === "user" ? "message own" : "message";
    item.dataset.messageId = msg.id;
    item.dataset.caseId = msg.case_id;
    const turn = turns.get(msg.turn_id);
    if (msg.role !== "user") item.append(el("strong", msg.source_refs ? "Отдел качества" : "Регламент бот"));
    const text = el("p");
    renderText(text, msg.text, msg.source_refs ?? (msg.role === "assistant" ? turn?.result?.sources : []) ?? []);
    item.append(text);
    if (msg.role === "assistant" && !msg.source_refs && turn) {
      const actions = el("div");
      actions.className = "actions";
      for (const clarification of turn.clarifications.filter((a) => a.status === "open")) {
        for (const option of clarification.options) {
          actions.append(button(option.text, (event) => perform(event.currentTarget, () =>
            send({case_id: clarification.case_id, clarification_id: clarification.id,
              clarification_version: clarification.version, option_id: option.id}))));
        }
      }
      for (const offer of turn.offers.filter((a) => a.status === "open")) {
        for (const [action, label] of [["confirm", "Отправить письмо"], ["decline", "Не отправлять"]]) {
          actions.append(button(label, (event) => perform(event.currentTarget, async () => {
            await api(`/v1/offers/${offer.id}/${action}`, {version: offer.version});
            await refresh();
          })));
        }
      }
      if (actions.childElementCount) item.append(actions);
    }
    const time = el("time", new Intl.DateTimeFormat("ru", {hour:"2-digit", minute:"2-digit"}).format(new Date(msg.created_at)));
    time.dateTime = msg.created_at;
    item.append(time);
    items.push(item);
  }
  for (const turn of history.turns) {
    if (turn.status === "technical_failure") {
      const item = el("article", "Ответ пока не готов. Вопрос сохранён.");
      item.className = "message";
      item.append(" ", button("Продолжить поиск", (event) => perform(event.currentTarget, async () => {
        await api(`/v1/turns/${turn.turn_id}/retry?attempt=${turn.attempt}`, {});
        await refresh();
      }, (message) => status(message ? "Пока не получается продолжить поиск. Вопрос сохранён." : ""))));
      items.push(item);
    }
  }
  for (const req of mailStates) {
    const item = el("article", labels[req.state] ?? req.state);
    item.className = "message mail-state";
    item.dataset.requestId = req.id;
    if (req.delivery === "delivery_unknown") item.textContent = "Проверяем результат доставки письма";
    items.push(item);
  }
  $("dion-messages").replaceChildren(...items);
  if (atBottom) scroll.scrollTop = scroll.scrollHeight;
  updateSearchStatus();
  $("message").placeholder = openClarification() ? "Ответьте на уточнение" : "Напишите сообщение";
}

function refreshCorrespondence() {
  if (correspondenceRefresh) return;
  correspondenceRefresh = api("/v1/browser/correspondence").then((next) => {
    if (JSON.stringify(mailStates) !== JSON.stringify(next)) {
      mailStates = next;
      render();
    }
  }).catch(() => {
    // Keep the latest correspondence snapshot until the next refresh.
  }).finally(() => { correspondenceRefresh = null; });
}

async function refresh() {
  refreshCorrespondence();
  if (refreshing) {
    needRefresh = true;
    return;
  }
  refreshing = true;
  try {
    const nextHistory = await api(`/v1/conversations/${conversation}/messages`);
    if (!history || history.cursor !== nextHistory.cursor) {
      history = nextHistory;
      render();
    }
  } finally {
    refreshing = false;
    if (needRefresh) {
      needRefresh = false;
      scheduleRefresh();
    }
  }
}

function scheduleRefresh() {
  if (refreshTimer != null) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refresh().catch(() => {});
  }, 0);
}

function watch() {
  stream?.close();
  stream = new EventSource(`/v1/conversations/${conversation}/events?after=${history.cursor}`);
  for (const type of ["turn.accepted", "turn.processing", "turn.completed", "turn.failed", "turn.technical_failure",
    "offer.confirmed", "offer.declined", "offer.superseded", "mail.resolution", "mail.counter_question",
    "mail.partial", "mail.withdrawal", "mail.followup_queued"]) {
    stream.addEventListener(type, scheduleRefresh);
  }
  stream.addEventListener("reset", async () => {
    stream.close();
    await refresh();
    watch();
  });
  stream.onerror = scheduleRefresh;
}

async function send(fields) {
  const key = `pending:${conversation}`;
  const previous = JSON.parse(sessionStorage.getItem(key) || "null");
  const same = previous && JSON.stringify(previous.fields) === JSON.stringify(fields);
  const command = same ? previous.command : crypto.randomUUID();
  sessionStorage.setItem(key, JSON.stringify({fields, command}));
  let result;
  try {
    result = await api(`/v1/conversations/${conversation}/messages`, {client_message_id: command, ...fields});
  } catch (error) {
    if (error.status && error.status < 500) sessionStorage.removeItem(key);
    throw error;
  }
  sessionStorage.removeItem(key);
  selectCase(result.case_id);
  await refresh();
}

$("compose").onsubmit = (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!text) return;
  const clarification = openClarification();
  const waiting = history?.mail_followups.find((req) => req.case_id === activeCase);
  perform($("compose").querySelector("button"), async () => {
    if (waiting) {
      await api(`/v1/mail/requests/${waiting.request_id}/followup`, {client_id: crypto.randomUUID(), text});
      await refresh();
    } else {
      await send({
        text, case_id: activeCase || null,
        ...(clarification ? {clarification_id: clarification.id, clarification_version: clarification.version} : {}),
      });
    }
    $("message").value = "";
  });
};
$("message").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !$("compose").querySelector("button").disabled) {
    event.preventDefault();
    $("compose").requestSubmit();
  }
};
$("new-topic").onclick = () => selectCase(null);

function loginForm() {
  const form = el("form");
  const loginLabel = el("label", "Логин");
  const login = el("input");
  login.name = "username";
  login.autocomplete = "username";
  login.required = true;
  loginLabel.append(login);
  const passLabel = el("label", "Пароль");
  const pass = el("input");
  pass.name = "password";
  pass.type = "password";
  pass.autocomplete = "current-password";
  pass.required = true;
  passLabel.append(pass);
  const submit = el("button", "Войти в общий ящик");
  submit.className = "primary";
  form.append(loginLabel, passLabel, submit);
  form.onsubmit = (e) => {
    e.preventDefault();
    perform(submit, async () => {
      await api("/v1/browser/mail/login", {login: login.value, password: pass.value});
      mail = true;
      await quality();
    }, mailStatus);
  };
  return form;
}

function inboxMessage(message) {
  const panel = el("details");
  panel.className = "panel";
  panel.dataset.messageId = message.message_id;
  const body = el("p", message.text);
  body.className = "mail-body";
  const form = el("form");
  const label = el("label", "Текст ответа");
  const textarea = el("textarea");
  textarea.required = true;
  textarea.maxLength = 8000;
  label.append(textarea);
  const submit = el("button", "Reply");
  submit.className = "primary";
  const state = el("p");
  state.setAttribute("role", "status");
  form.append(label, submit, state);
  let pending;
  form.onsubmit = (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text) return;
    pending = pending?.text === text ? pending : {command_id: crypto.randomUUID(), message_id: message.message_id, text};
    perform(submit, async () => {
      const accepted = await api("/v1/browser/mail/reply", pending);
      state.textContent = "Ответ поставлен в очередь";
      for (let i = 0; i < 45; i++) {
        const result = await api(`/v1/browser/mail/reply/${accepted.command_id}`);
        if (result.state === "sent") {
          state.textContent = "Ответ отправлен";
          textarea.value = "";
          pending = null;
          return;
        }
        if (result.state === "failed" || result.state === "uncertain") {
          state.textContent = "Доставка ответа не подтверждена";
          return;
        }
        await new Promise((done) => setTimeout(done, 1000));
      }
    }, (error) => { state.textContent = error; });
  };
  panel.append(el("summary", message.subject), el("small", `От: ${message.sender} · Копия: ${message.cc}`), body,
    el("p", "Содержательный ответ из общего ящика может стать общим разъяснением."), form);
  return panel;
}

async function quality() {
  const version = ++viewVersion;
  const area = $("quality-content");
  mailStatus("");
  for (const key of ["inbox", "faq", "sources"]) {
    const ctl = $(`tab-${key}`);
    if (tab === key) ctl.setAttribute("aria-current", "page");
    else ctl.removeAttribute("aria-current");
  }
  if (tab === "inbox" && !mail) {
    area.replaceChildren(loginForm());
    return;
  }
  area.replaceChildren(el("p", "Загрузка…"));
  try {
    if (tab === "inbox") {
      const messages = await api("/v1/browser/mail/inbox");
      if (version !== viewVersion) return;
      const logout = button("Выйти из почты", (event) => perform(event.currentTarget, async () => {
        await api("/v1/browser/mail/logout", {});
        mail = false;
        await quality();
      }, mailStatus));
      const refreshBox = button("Обновить входящие", () => quality());
      area.replaceChildren(logout, " ", refreshBox, ...(messages.length ? messages.map(inboxMessage) : [el("p", "Входящих писем пока нет.")]));
    } else {
      const data = await api("/v1/browser/catalog");
      if (version !== viewVersion) return;
      if (tab === "faq") {
        area.replaceChildren(...(data.faq.length ? data.faq.map((ref) => {
          const card = el("article");
          card.className = "panel";
          card.append(el("p", ref.quote), sourceLink(ref, "Открыть разъяснение"));
          return card;
        }) : [el("p", "Опубликованных разъяснений пока нет.")]));
      } else {
        area.replaceChildren(...data.documents.map((revisions) => {
          const group = el("details");
          group.className = "panel";
          group.append(el("summary", revisions[0].title));
          revisions.forEach((ref) => {
            const link = sourceLink(ref, `Пункт ${ref.anchor}`);
            link.className = "catalog-link";
            group.append(link);
          });
          return group;
        }));
      }
    }
  } catch (error) {
    if (version !== viewVersion) return;
    if (error.status === 401 && tab === "inbox") {
      mail = false;
      area.replaceChildren(loginForm());
    } else {
      area.replaceChildren(button("Повторить", () => quality()));
    }
    mailStatus(error.message);
  }
}

$("nav-quality").onclick = () => {
  $("dion-desk").hidden = true;
  $("quality").hidden = false;
  $("nav-chat").removeAttribute("aria-current");
  $("nav-quality").setAttribute("aria-current", "page");
  quality();
};
$("nav-chat").onclick = () => {
  $("quality").hidden = true;
  $("dion-desk").hidden = false;
  $("nav-quality").removeAttribute("aria-current");
  $("nav-chat").setAttribute("aria-current", "page");
  scheduleRefresh();
};
for (const key of ["inbox", "faq", "sources"]) {
  $(`tab-${key}`).onclick = () => { tab = key; quality(); };
}

try {
  const session = await api("/v1/browser/session", {});
  conversation = session.conversation_id;
  mail = session.mail;
  activeCase = localStorage.getItem(`case:${conversation}`);
  await refresh();
  watch();
  // Polling also restores a snapshot when the server no longer retains the SSE cursor.
  setInterval(scheduleRefresh, 3000);
  const pending = JSON.parse(sessionStorage.getItem(`pending:${conversation}`) || "null");
  if (pending) await send(pending.fields);
} catch (error) {
  status(error.message);
}
