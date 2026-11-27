const $ = (id) => document.getElementById(id);
const rubles = new Intl.NumberFormat("ru-RU", {style: "currency", currency: "RUB", maximumFractionDigits: 4});
const count = new Intl.NumberFormat("ru-RU");
const shownCount = (value) => value == null ? "—" : count.format(value);
const shownCost = (usd) => usd == null ? "—" : rubles.format(Number(usd) * 85);
const states = {
  queued: "В очереди", pending: "В очереди", running: "Выполняется", processing: "Выполняется",
  started: "Выполняется", completed: "Готово", succeeded: "Готово", failed: "Ошибка",
  technical_failure: "Обработка прервана", sent: "Письмо отправлено", uncertain: "Доставка не подтверждена",
  delivery_unknown: "Доставка не подтверждена", waiting_send: "Письмо в очереди",
};
const roles = {search: "Поиск A1 / MiniLM", judge: "Проверка оснований", answer: "Подготовка ответа", route: "Маршрутизация"};

export function chatTools({api, openCitation, openInbox, applyDraft}) {
  let hint;
  let hintRequest = 0;
  let logRequest = 0;
  async function loadHint() {
    const current = ++hintRequest;
    try {
      const next = await api("/v1/browser/hint");
      if (current !== hintRequest) return;
      hint = next;
      $("dion-hint").hidden = next.stage === "done" || !next.text;
      $("dion-hint-text").textContent = next.text || "";
      $("dion-hint-example").textContent = next.example || "";
      const open = next.action === "open";
      $("dion-hint-apply").hidden = open ? !next.source : next.action !== "example" || !next.example;
      $("dion-hint-apply").textContent = open ? "Открыть пункт" : "Применить";
      $("dion-hint-inbox").hidden = next.action !== "inbox" || !next.thread_id;
    } catch {
      if (current === hintRequest) {
        hint = null;
        $("dion-hint").hidden = true;
      }
    }
  }
  $("dion-hint-apply").onclick = () => {
    if (hint?.action === "open" && hint.source) openCitation(hint.source);
    else if (hint?.example) applyDraft(hint);
  };
  $("dion-hint-inbox").onclick = () => { if (hint?.thread_id) openInbox(hint.thread_id); };
  function logRow(title, detail) {
    const row = document.createElement("li");
    row.className = "event";
    const heading = document.createElement("strong");
    const info = document.createElement("p");
    heading.textContent = title;
    info.textContent = detail;
    row.append(heading, info);
    return row;
  }
  function clearLog(label) {
    $("trace-status").textContent = label;
    $("trace-id").textContent = "";
    $("trace-stats").hidden = true;
    $("trace-rate").hidden = true;
    $("events").replaceChildren();
  }
  async function loadLog(clear = false) {
    const current = ++logRequest;
    if ($("trace").hidden) return;
    if (clear) clearLog("Загрузка…");
    try {
      const log = await api("/v1/browser/last-log");
      if (current !== logRequest) return;
      if (!log.turn_id) { clearLog("Здесь появятся этапы обработки вашего первого вопроса."); return; }
      $("trace-status").textContent = [states[log.status] || log.status, log.incomplete && "Итог неполный"].filter(Boolean).join(" · ");
      const traceId = log.steps.find((step) => step.trace_id)?.trace_id;
      $("trace-id").textContent = traceId || log.turn_id;
      $("trace-rate").hidden = false;
      $("trace-stats").hidden = false;
      const calls = log.steps.filter((step) => !step.local);
      const known = (field) => !calls.length || calls.some((step) => step[field] != null);
      const input = known("input_tokens") ? log.total_input_tokens : null;
      const output = known("output_tokens") ? log.total_output_tokens : null;
      $("tokens").textContent = shownCount(input) + " вх. / " + shownCount(output) + " исх.";
      $("trace-cost").textContent = shownCost(known("cost_usd") ? log.total_cost_usd : null);
      const rows = log.steps.map((step) => {
        const details = [step.model, step.provider, states[step.status] || step.status];
        if (step.local) details.push("локально");
        else details.push("Вход " + shownCount(step.input_tokens), "выход " + shownCount(step.output_tokens), shownCost(step.cost_usd));
        return logRow(roles[step.role] || step.role, details.filter(Boolean).join(" · "));
      });
      if (log.mail) {
        const mail = log.mail;
        const title = states[mail.state] || mail.state;
        rows.push(logRow(title + " · Обращение ОК" + mail.number, [
          ["От", mail.sender], ["Кому", mail.recipient], ["Копия", mail.cc],
        ].filter(([, value]) => value).map(([label, value]) => label + ": " + value).join(" · ")));
      }
      $("events").replaceChildren(...rows);
    } catch {
      if (current === logRequest) clearLog("Не удалось загрузить журнал.");
    }
  }
  function setLogs(open, focus = false) {
    $("trace").hidden = !open;
    $("show-logs").setAttribute("aria-expanded", String(open));
    if (open) {
      loadLog(true);
      if (focus) $("close-logs").focus();
    } else {
      ++logRequest;
      if (focus) $("show-logs").focus();
    }
  }
  $("show-logs").onclick = () => setLogs($("trace").hidden, true);
  $("close-logs").onclick = () => setLogs(false, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("trace").hidden && !document.querySelector("dialog[open]")) setLogs(false, true);
  });
  return {loadHint, loadLog, setLogs};
}
