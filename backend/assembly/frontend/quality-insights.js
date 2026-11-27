import {button, empty, node, safeLink, select} from "./quality-ui.js";

function table(headers, rows, emptyMessage) {
  if (!rows.length) return empty(emptyMessage);
  const header = node("tr");
  for (const title of headers) {
    const cell = node("th", "", title);
    cell.scope = "col";
    header.append(cell);
  }
  const body = node("tbody");
  for (const values of rows) body.append(node("tr", "", ...values.map((value) => node("td", "", value))));
  return node("table", "", node("thead", "", header), body);
}

export function createInsights({api, requireExpert, run, openRegulation}) {
  let order = "high";
  function docLink(doc, label) {
    return button(label, () => openRegulation({
      document_id: doc.document_id, revision_id: doc.revision_id,
      fragment_id: doc.fragment_id, anchor: doc.anchor,
    }), "quality-link");
  }
  async function sources() {
    const docs = await api("/v1/browser/documents");
    const documents = node("ul");
    for (const doc of docs) {
      const row = node("li", "", docLink(doc, doc.title));
      if (doc.original_url) row.append(" · ", safeLink("Оригинал", doc.original_url));
      documents.append(row);
    }
    const list = node("ol", "quality-source-list");
    const catalog = [
      ["Confluence (демо)", "Действующая редакция регламентов определяет инструкцию."],
      ["Разъяснения отдела", "Почта Отдела качества (quality@assembly.test). Доверенные письма уточняют применение регламентов."],
      ["Яндекс.Диск", "Пример дополнительного источника."],
    ];
    function draw() {
      const rows = catalog.map(([name, description], i) => {
        const info = node("div", "", node("strong", "", name), node("p", "", description));
        if (i === 0) info.append(node("details", "source-regulations", node("summary", "", "Регламенты"),
          docs.length ? documents : empty("Доступных регламентов пока нет.")));
        const enabled = i !== 2;
        const row = node("li", "quality-source", info, node("div", "quality-source-meta",
          "Приоритет " + (i + 1), node("br"), enabled ? "Подключён" : "Отключён"));
        row.dataset.enabled = String(enabled);
        return row;
      });
      list.replaceChildren(...(order === "high" ? rows : rows.reverse()));
    }
    draw();
    return node("div", "", node("div", "quality-toolbar", node("div", "quality-source-controls",
      select("Приоритет", "source-order", {high: "Сначала высокий", low: "Сначала низкий"}, order,
        (value) => { order = value; draw(); }),
      node("p", "quality-source-note", "Если применимые источники расходятся, приоритет определяет, на какой из них бот опирается в ответе. Конфликты проверяет модель. Результат виден в разъяснениях.")),
    node("span", "note", "Просмотр")), list, node("p", "quality-source-note",
      "Общий модуль импорта загружает и индексирует документы. Для новых подключений предусмотрена спецификация выгрузки, проверки подключения и обновления по расписанию."));
  }
  function groupRow(group) {
    const detail = node("ul", "quality-questions");
    detail.hidden = true;
    const ctl = button(group.topic, () => requireExpert(() => run(ctl, async () => {
      if (!detail.hidden) {
        detail.hidden = true;
        ctl.setAttribute("aria-expanded", "false");
        return;
      }
      const questions = await api("/v1/browser/statistics/groups/" + encodeURIComponent(group.id) + "/questions");
      detail.replaceChildren(...questions.map((question) => node("li", "", question.question)));
      detail.hidden = false;
      ctl.setAttribute("aria-expanded", "true");
    })), "quality-link");
    ctl.setAttribute("aria-expanded", "false");
    const conditions = Object.values(group.conditions || {}).join(" · ");
    return [node("div", "", ctl, detail), conditions || "—", group.count];
  }
  async function stats() {
    const stats = await api("/v1/browser/statistics");
    const section = (title, ...parts) => node("section", "quality-analytics-card",
      node("h3", "", title), ...parts);
    return node("div", "quality-analytics",
      node("p", "quality-source-note", "Выдачи, пробелы, повторы и оценки — последние 30 календарных дней, МСК. Расхождения — по последней проверке карточек."),
      section("Востребованные регламенты", table(["Регламент", "Выдачи"],
        stats.documents.map((doc) => [docLink(doc, doc.title), doc.issues]), "За период ещё не было выдач.")),
      section("Проблемные пункты", node("p", "note", "Всего пробелов за период — " + stats.gaps_total),
        table(["Регламент и редакция", "Пробелы", "Расхождения"], stats.problems.map((doc) => [
          doc.document_id ? docLink(doc, doc.title + " · п. " + doc.anchor + " · ред. " + doc.version)
            : "Без привязки к пункту", doc.gaps, doc.conflicts,
        ]), "За период нет пробелов и подтверждённых расхождений.")),
      section("Повторяющиеся вопросы без ответа", table(["Тема", "Условия", "Обращения"],
        stats.groups.map(groupRow), "За период нет повторяющихся вопросов без ответа.")),
      section("Оценки ответов", node("p", "quality-ratings", node("span", "", "👍 Помогло · " + stats.ratings.up),
        node("span", "", "👎 Не помогло · " + stats.ratings.down))));
  }
  return {sources, stats};
}
