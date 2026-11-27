import {actions, block, button, byId, empty, node, select, time} from "./quality-ui.js";
import {renderMessage} from "./quality-mail.js";

const states = {active: "Действует", review: "Требует пересмотра", withdrawn: "Отменена"};
const stateOrder = {review: 0, active: 1, withdrawn: 2};
const outcomes = {conflict: "Есть противоречие", consistent: "Нормы согласуются",
  unconfirmed: "Конфликт не подтверждён", no_source: "Подходящего источника нет"};
const usesForm = new Intl.PluralRules("ru");
const historyActions = {
  published: "Публикация", edit: "Изменение текста", withdraw: "Отмена",
  resume: "Возобновление", review: "Проверка",
};
const cardsUrl = "/v1/browser/quality/cards";

export function createCards({api, requireExpert, run, status, openSource, refresh, openInbox}) {
  let filter = "all";
  let expanded = null;
  let editing;
  let report;
  let scanId;
  const dialog = byId("faq-edit-dialog");

  function evidenceView(evidence) {
    if (!evidence) return null;
    const container = node("div", evidence.outcome === "conflict" ? "desk-alert" : "desk-block",
      node("strong", "", outcomes[evidence.outcome] || "Проверка карточки"));
    if (evidence.reason) container.append(node("p", "quality-copy", evidence.reason));
    if (evidence.quote) container.append(node("blockquote", "quality-copy", evidence.quote));
    if (evidence.url) {
      const link = button([evidence.title, evidence.anchor && "п. " + evidence.anchor].filter(Boolean).join(" · ")
        || "Открыть пункт", () => openSource(evidence), "quality-link");
      container.append(link);
    }
    return container;
  }
  function resetWarning() {
    byId("faq-edit-warning").hidden = true;
    byId("faq-edit-confirm").hidden = true;
    byId("faq-edit-save").hidden = false;
  }
  function edit(card) {
    editing = card;
    byId("faq-edit-question").value = card.question;
    byId("faq-edit-answer").value = card.answer;
    resetWarning();
    dialog.showModal();
    byId("faq-edit-question").focus();
  }
  function warn(message, evidence) {
    const warning = byId("faq-edit-warning");
    warning.replaceChildren(node("p", "", message));
    if (evidence) {
      warning.append(block("Редактируемый ответ", byId("faq-edit-answer").value), evidenceView(evidence));
    }
    warning.hidden = false;
  }
  function save(confirm, ctl) {
    run(ctl, async () => {
      const question = byId("faq-edit-question").value.trim();
      const answer = byId("faq-edit-answer").value.trim();
      if (!question || !answer) return warn("Заполните вопрос и ответ.");
      try {
        await api(cardsUrl + "/" + editing.id + "/edit", {
          version: editing.version, question, answer, confirm_conflict: confirm,
        });
      } catch (error) {
        if (error.status === 409 && error.detail?.code === "confirm_conflict") {
          warn("Есть противоречие. Уверены, что хотите сохранить?", error.detail.evidence);
          byId("faq-edit-confirm").hidden = false;
          byId("faq-edit-save").hidden = true;
          return;
        }
        if (error.status === 409) {
          warn("Карточка уже обновилась. Закройте окно и откройте актуальную версию перед сохранением.");
          await refresh();
          return;
        }
        throw error;
      }
      dialog.close();
      await refresh();
      status("Карточка сохранена.");
    }, warn);
  }
  byId("faq-edit-save").onclick = (event) => save(false, event.currentTarget);
  byId("faq-edit-confirm").onclick = (event) => save(true, event.currentTarget);
  for (const field of ["faq-edit-question", "faq-edit-answer"]) byId(field).oninput = resetWarning;

  async function savedMail(card) {
    const thread = await api("/v1/browser/quality/threads/" + encodeURIComponent(card.request_id));
    byId("faq-thread-subject").textContent = thread.subject;
    byId("faq-thread-messages").replaceChildren(...thread.messages.map((item) => renderMessage(item, true)));
    byId("faq-thread-dialog").showModal();
  }
  function cardContent(card) {
    const body = node("div", "faq-row-answer", block("Ответ", card.answer));
    const evidence = evidenceView(card.evidence);
    if (evidence) body.append(evidence);
    if (card.history?.length) {
      const list = node("ul", "desk-timeline");
      for (const version of [...card.history].sort((a, b) => b.version - a.version)) {
        list.append(node("li", "", node("strong", "", "Редакция " + version.version), " · ", time(version.at),
          node("p", "note", [historyActions[version.action], version.actor].filter(Boolean).join(" · ")),
          block("Вопрос", version.question), block("Ответ", version.answer)));
      }
      body.append(node("details", "desk-block", node("summary", "", "История изменений"), list));
    }
    const changeStatus = async (action) => {
      try {
        await api(cardsUrl + "/" + card.id + "/" + action, {version: card.version});
      } catch (error) {
        if (error.status === 409) await refresh();
        throw error;
      }
      await refresh();
    };
    const confirm = node("div", "desk-confirm",
      node("p", "", "Отменить карточку? Бот перестанет использовать её в новых ответах. Переписка и история сохранятся."));
    confirm.hidden = true;
    const approve = button("Подтвердить отмену", (ctl) => run(ctl, () => changeStatus("withdraw")));
    approve.dataset.qualityAction = "cancel-confirm";
    const lifecycle = card.status === "withdrawn" ?
      button("Возобновить", (ctl) => requireExpert(() => run(ctl, () => changeStatus("resume")))) :
      button("Отменить карточку", () => requireExpert(() => { confirm.hidden = false; approve.focus(); }));
    lifecycle.dataset.qualityAction = card.status === "withdrawn" ? "resume" : "cancel";
    confirm.append(actions(approve, button("Назад", () => { confirm.hidden = true; lifecycle.focus(); })));
    const controls = actions(lifecycle);
    if (card.status !== "withdrawn") controls.append(button("Редактировать", () => requireExpert(() => edit(card))));
    controls.append(button("Открыть всю переписку", (ctl) => run(ctl, () => savedMail(card))));
    body.append(controls, confirm);
    return body;
  }
  function row(card, draw) {
    const count = card.uses ?? 0;
    const usage = "Использовано " + count + (usesForm.select(count) === "few" ? " раза" : " раз");
    const trigger = button("", () => {
      expanded = expanded === card.id ? null : card.id;
      draw();
      byId("faq-row-" + card.id)?.focus();
    }, "faq-row-button");
    trigger.id = "faq-row-" + card.id;
    trigger.setAttribute("aria-expanded", String(expanded === card.id));
    trigger.setAttribute("aria-controls", "faq-details-" + card.id);
    const badge = node("span", "quality-pill", states[card.status]);
    badge.dataset.state = card.status === "withdrawn" ? "cancelled" : card.status;
    trigger.append(node("span", "", node("strong", "faq-row-question", card.question),
      node("span", "faq-row-id", card.id + " · " + usage)),
    node("span", "faq-row-dates", node("span", "", "Создано ", time(card.created_at)),
      node("span", "", "Обновлено ", time(card.updated_at))), badge, node("span", "faq-chevron", "⌄"));
    const detail = expanded === card.id ? cardContent(card) : node("div");
    detail.hidden = expanded !== card.id;
    detail.id = "faq-details-" + card.id;
    return node("li", "faq-row", trigger, detail);
  }
  async function render() {
    const cards = await api(cardsUrl);
    const list = node("ul", "faq-list");
    list.setAttribute("aria-label", "Разъяснения отдела");
    const noEntries = empty("Карточек с таким статусом нет.");
    function draw() {
      const matching = cards.filter((card) => filter === "all" || card.status === filter)
        .sort((a, b) => stateOrder[a.status] - stateOrder[b.status]
          || Date.parse(b.updated_at) - Date.parse(a.updated_at) || String(a.id).localeCompare(String(b.id)));
      noEntries.hidden = matching.length > 0;
      list.replaceChildren(...matching.map((card) => row(card, draw)));
    }
    const view = node("div", "", node("div", "quality-toolbar",
      select("Статус", "faq-filter", {all: "Все", ...states}, filter, (value) => { filter = value; draw(); }),
      button("Проверить конфликты", (ctl) => run(ctl, async () => {
        scanId ??= crypto.randomUUID();
        report = await api("/v1/browser/quality/checks", {command_id: scanId});
        scanId = null;
        await refresh();
        status("Проверка завершена.");
      }), "button primary")));
    if (report) {
      const delivery = {pending: "Отчёт сохранён и ожидает отправки.",
        sent: "Отчёт отправлен в учебный ящик.", failed: "Отправка отчёта не удалась.",
        delivery_unknown: "Доставка отчёта пока не подтверждена."};
      view.append(node("div", "quality-op-summary", node("strong", "", report.subject),
        node("p", "quality-copy", report.text), node("p", "note", delivery[report.delivery] || ""),
        button("Открыть письмо с итогами", () => openInbox(report.id))));
    }
    draw();
    view.append(list, noEntries, node("p", "quality-faq-note",
      "Карточки добавляются при ответах по почте от Отдела качества. ИИ-судья проверяет, что это итоговый ответ на вопрос, и сверяет его с регламентом. При конфликте бот показывает оба ответа до отзыва карточки или обновления регламента. Например, «В регламенте указано 5 дней, а в разъяснении — 3 дня. Стоит перепроверить»."));
    return view;
  }
  return {render};
}
