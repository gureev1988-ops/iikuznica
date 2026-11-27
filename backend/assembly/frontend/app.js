import {documentReader} from "./documents.js";
import {chatTools} from "./chat-tools.js";
import {initQuality} from "./quality.js";

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
let quality;
let view = "dion";
let ratings = new Map();
let ratingRequest = 0;
let stream;
let refreshTimer;
let deeperSearchTimer;
let refreshing = false;
let needRefresh = false;
let correspondenceRefresh;

const status = (message) => { $("chat-status").textContent = message; };

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

async function api(path, body, method = "POST") {
  const options = body === undefined ? {} : {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  };
  const response = await fetch(path, {...options, cache: "no-store"});
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    const error = Error(typeof data.detail === "string" ? data.detail : (data.detail?.code || "Ошибка запроса"));
    error.status = response.status;
    error.detail = data.detail;
    throw error;
  }
  return data;
}

function chatNotice(message) {
  if (!message) status("");
  else if (message === "interactive_queue_full") status("Сейчас много вопросов. Попробуйте отправить чуть позже.");
  else if (typeof message === "object" && message.detail?.message) status(message.detail.message);
  else status("Пока не получается выполнить действие. Попробуйте ещё раз.");
}

function perform(node, operation, report = chatNotice) {
  node.disabled = true;
  report("");
  return Promise.resolve().then(operation).catch((error) => report(error.detail?.message ? error : error.message))
    .finally(() => { node.disabled = false; });
}

const reader = documentReader({api, onCitation: async (fragmentId) => {
  try {
    await api("/v1/browser/hint/open", {fragment_id: fragmentId});
    guide.loadHint();
  } catch {
    // The source remains readable if the guide is temporarily unavailable.
  }
}});
const guide = chatTools({
  api, openCitation: (ref) => reader.openSource(ref, {fromChat: true}),
  openInbox: (threadId) => activate("quality", {threadId}),
  applyDraft: (example) => {
    $("message").value = example;
    saveDraft();
    $("message").focus();
  },
});

function sourceLink(ref, label) {
  const link = el("a", ref.claim === "Открыть регламент" ? "Открыть регламент" : label);
  link.href = ref.claim === "Открыть регламент" ? reader.documentHash(ref) : ref.url;
  link.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    reader.openSource(ref, {fromChat: true});
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

function draftKey() { return "draft:" + conversation + ":" + (activeCase || "new"); }
function saveDraft() {
  if (!conversation) return;
  if ($("message").value) sessionStorage.setItem(draftKey(), $("message").value);
  else sessionStorage.removeItem(draftKey());
}
$("message").oninput = saveDraft;

function selectCase(value) {
  saveDraft();
  activeCase = value;
  if (value) localStorage.setItem(`case:${conversation}`, value);
  else localStorage.removeItem(`case:${conversation}`);
  $("message").value = sessionStorage.getItem(draftKey()) || "";
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
  const latestTurns = new Map(history.turns.map((turn) => [turn.case_id, turn.turn_id]));
  const topics = new Map();
  for (const turn of history.turns) if (!topics.has(turn.case_id)) topics.set(turn.case_id, turn.question);
  const topicNodes = [...topics].map(([id, title]) => {
    const ctl = button(title, () => selectCase(id));
    ctl.title = title;
    ctl.className = "topic-link";
    if (activeCase === id) ctl.setAttribute("aria-current", "true");
    return ctl;
  });
  $("topics").replaceChildren(...topicNodes);
  const items = [];
  $("dion-last").textContent = history.messages.at(-1)?.text || "Бот";
  for (const msg of history.messages) {
    const item = el("article");
    item.className = msg.role === "user" ? "dion-message dion-own" : "dion-message";
    item.dataset.messageId = msg.id;
    item.dataset.caseId = msg.case_id;
    const turn = turns.get(msg.turn_id);
    if (msg.role !== "user") {
      const name = el("strong", msg.source_refs ? "Отдел качества" : "Регламент бот");
      name.className = "dion-name";
      item.append(name);
    }
    const text = el("p");
    let visibleText = msg.text;
    if (msg.role === "assistant" && !msg.source_refs) {
      // Earlier turns stored the prompt and displayed its persisted options as buttons.
      const oldList = turn?.clarifications.find((c) => c.prompt === visibleText && c.options.length);
      if (oldList) {
        const choices = oldList.options.map((option, index) => (index + 1) + ". " + option.text);
        visibleText += "\n\n" + choices.join("\n") + "\n\nМожете ответить просто цифрой.";
      }
    }
    renderText(text, visibleText, msg.source_refs ?? (msg.role === "assistant" ? turn?.result?.sources : []) ?? []);
    item.append(text);
    if (msg.role === "assistant" && !msg.source_refs && turn?.result?.notice) {
      const notice = el("p", turn.result.notice);
      notice.className = "result-notice";
      item.append(notice);
    }
    if (ratings.has(msg.id)) item.append(ratingButtons(msg.id));
    const time = el("time", new Intl.DateTimeFormat("ru", {hour:"2-digit", minute:"2-digit"}).format(new Date(msg.created_at)));
    time.className = "dion-time";
    time.dateTime = msg.created_at;
    item.append(time);
    items.push(item);
  }
  for (const turn of history.turns) {
    if (turn.status === "technical_failure" && latestTurns.get(turn.case_id) === turn.turn_id) {
      const item = el("article", "Ответ пока не готов. Вопрос сохранён.");
      item.className = "dion-message";
      item.append(" ", button("Продолжить поиск", (event) => perform(event.currentTarget, async () => {
        await api(`/v1/turns/${turn.turn_id}/retry?attempt=${turn.attempt}`, {});
        await refresh();
      }, (message) => status(message ? "Пока не получается продолжить поиск. Вопрос сохранён." : ""))));
      items.push(item);
    }
  }
  for (const req of mailStates) {
    const item = el("article", labels[req.state] ?? req.state);
    item.className = "dion-message mail-state";
    item.dataset.requestId = req.id;
    if (req.delivery === "delivery_unknown") item.textContent = "Проверяем результат доставки письма";
    if (req.number ?? req.case_number) item.prepend("Обращение ОК" + (req.number ?? req.case_number) + " · ");
    items.push(item);
  }
  $("dion-messages").replaceChildren(...items);
  if (atBottom) scroll.scrollTop = scroll.scrollHeight;
  updateSearchStatus();
  $("message").placeholder = openClarification() ? "Ответьте на уточнение" : "Напишите сообщение";
}

async function loadRatings() {
  const current = ++ratingRequest;
  try {
    const rows = await api("/v1/browser/conversations/" + conversation + "/ratings");
    if (current !== ratingRequest) return;
    const next = new Map(rows.map((item) => [item.message_id, item.rating]));
    if (next.size === ratings.size && [...next].every(([key, val]) => ratings.get(key) === val && ratings.has(key))) return;
    ratings = next;
    render();
  } catch {
    // A rating is offered only after the server confirms eligibility.
  }
}

const voting = new Set();
function ratingButtons(messageId) {
  const wrap = el("div");
  wrap.className = "answer-votes";
  for (const [rating, icon, label] of [["up", "👍", "Помогло"], ["down", "👎", "Не помогло"]]) {
    const vote = button(icon, async () => {
      if (voting.has(messageId)) return;
      voting.add(messageId);
      for (const node of wrap.children) node.disabled = true;
      try {
        const result = await api("/v1/browser/messages/" + messageId + "/rating", {rating}, "PUT");
        ++ratingRequest;
        ratings.set(messageId, result.rating);
      } catch {
        status("Не удалось сохранить оценку. Попробуйте ещё раз.");
      } finally {
        voting.delete(messageId);
        const group = document.querySelector('[data-message-id="' + messageId + '"] .answer-votes');
        if (group) {
          group.replaceWith(ratingButtons(messageId));
          const active = document.querySelector('[data-message-id="' + messageId + '"] [data-vote="' + rating + '"]');
          active?.focus({preventScroll: true});
        }
      }
    });
    vote.dataset.vote = rating;
    vote.disabled = voting.has(messageId);
    vote.setAttribute("aria-label", label);
    vote.title = label;
    vote.setAttribute("aria-pressed", String(ratings.get(messageId) === rating));
    wrap.append(vote);
  }
  return wrap;
}

function refreshCorrespondence() {
  if (correspondenceRefresh) return;
  correspondenceRefresh = api("/v1/browser/correspondence").then((next) => {
    if (JSON.stringify(mailStates) !== JSON.stringify(next)) {
      mailStates = next;
      render();
      guide.loadHint();
      guide.loadLog();
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
      const newTurn = history?.turns.at(-1)?.turn_id !== nextHistory.turns.at(-1)?.turn_id;
      history = nextHistory;
      render();
      loadRatings();
      guide.loadHint();
      guide.loadLog(newTurn);
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
  if (activeCase === (fields.case_id || null) && activeCase !== result.case_id) {
    activeCase = result.case_id;
    localStorage.setItem("case:" + conversation, activeCase);
  }
  await refresh();
}

async function sendMailFollowup(waiting, text) {
  const key = "followup:" + waiting.request_id;
  const previous = JSON.parse(sessionStorage.getItem(key) || "null");
  const command = previous?.text === text ? previous : {client_id: crypto.randomUUID(), text};
  sessionStorage.setItem(key, JSON.stringify(command));
  try {
    await api("/v1/mail/requests/" + waiting.request_id + "/followup", command);
    sessionStorage.removeItem(key);
  } catch (error) {
    if (error.status && error.status < 500) sessionStorage.removeItem(key);
    throw error;
  }
  await refresh();
}

$("compose").onsubmit = (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!text) return;
  if (!history || $("dion-send").disabled) return;
  const submittedKey = draftKey();
  const clarification = openClarification();
  const waiting = history?.mail_followups.find((req) => req.case_id === activeCase);
  perform($("compose").querySelector("button"), async () => {
    if (waiting) {
      await sendMailFollowup(waiting, text);
    } else {
      await send({
        text, case_id: activeCase || null,
        ...(clarification ? {clarification_id: clarification.id, clarification_version: clarification.version} : {}),
      });
    }
    if ($("message").value.trim() === text) {
      $("message").value = "";
      sessionStorage.removeItem(submittedKey);
      saveDraft();
    }
  });
};
$("message").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !$("compose").querySelector("button").disabled) {
    event.preventDefault();
    $("compose").requestSubmit();
  }
};
$("new-topic").onclick = () => selectCase(null);

function activate(nextView, options) {
  view = nextView;
  const deskId = {dion: "dion-desk", quality: "quality-desk", admin: "admin-desk"};
  for (const nav of document.querySelectorAll("#product-nav [data-view]")) {
    const selected = nav.dataset.view === nextView;
    $(deskId[nav.dataset.view]).hidden = !selected;
    if (selected) {
      nav.setAttribute("aria-current", "page");
      $("view-label").textContent = nav.textContent;
      document.title = "ИИкузница · " + nav.textContent;
    } else nav.removeAttribute("aria-current");
  }
  $("product-header").dataset.theme = nextView;
  if (nextView !== "dion") guide.setLogs(false);
  if (nextView === "quality") quality?.open(options);
  if (nextView === "dion" && conversation) {
    scheduleRefresh();
    guide.loadHint();
    loadRatings();
  }
}

for (const nav of document.querySelectorAll("#product-nav [data-view]")) {
  nav.onclick = () => activate(nav.dataset.view);
}
function techAnchor() {
  if (!/^#tech-[a-z]+$/.test(location.hash)) return;
  const block = document.getElementById(location.hash.slice(1));
  if (block && $("admin-desk").contains(block)) {
    activate("admin");
    block.scrollIntoView({block: "start"});
  }
}
window.addEventListener("hashchange", techAnchor);
techAnchor();

try {
  const session = await api("/v1/browser/session", {});
  conversation = session.conversation_id;
  if (session.standard_demo_login) {
    $("expert-access-note").textContent = "Учебный доступ · логин admin, пароль admin.";
  }
  quality = initQuality({
    api, openSource: reader.openSource, openRegulation: reader.openRegulation, expert: session.mail,
    onRoleChange: () => { if (view === "dion") scheduleRefresh(); },
  });
  if (view === "quality") quality.open();
  activeCase = localStorage.getItem("case:" + conversation);
  $("message").value = sessionStorage.getItem(draftKey()) || "";
  await refresh();
  watch();
  reader.readDocument();
  // Polling recovers an expired SSE cursor and asynchronous mail delivery.
  setInterval(scheduleRefresh, 3000);
  const pending = JSON.parse(sessionStorage.getItem("pending:" + conversation) || "null");
  if (pending) {
    await send(pending.fields);
    if ($("message").value.trim() === pending.fields.text) {
      $("message").value = "";
      saveDraft();
    }
  }
  $("dion-send").disabled = false;
} catch (error) {
  chatNotice(error.message);
  if (history) $("dion-send").disabled = false;
}
