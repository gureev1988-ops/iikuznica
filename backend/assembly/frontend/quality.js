import {byId, empty} from "./quality-ui.js";
import {createCards} from "./quality-cards.js";
import {createMail} from "./quality-mail.js";
import {createInsights} from "./quality-insights.js";

const titles = {
  faq: "Разъяснения", inbox: "Почта Отдела качества",
  sources: "Источники", analytics: "Статистика",
};

export function initQuality({api, openSource, openRegulation, onRoleChange, expert = false}) {
  const content = byId("quality-content");
  const login = byId("expert-dialog");
  byId("mail-search").maxLength = 300;
  const status = (text = "") => { byId("quality-status").textContent = text; };
  let current = "faq";
  let generation = 0;
  let afterLogin;

  function role(next) {
    expert = next;
    onRoleChange?.(next);
    byId("quality-role").textContent = expert ? "Эксперт · демовход" : "Гость · просмотр";
    byId("quality-login").textContent = expert ? "Выйти" : "Войти";
    byId("quality-login").setAttribute("aria-label", expert ? "Выйти из режима эксперта" : "Войти как эксперт");
  }
  function promptLogin(action) {
    afterLogin = action;
    byId("expert-error").hidden = true;
    byId("expert-user").value = "";
    byId("expert-password").value = "";
    if (!login.open) login.showModal();
    byId("expert-user").focus();
  }
  function requireExpert(action) {
    return expert ? action() : promptLogin(action);
  }
  async function run(ctl, action, report = status) {
    if (ctl.disabled) return;
    ctl.disabled = true;
    try {
      await action();
    } catch (error) {
      if (error.status === 401 && expert) {
        role(false);
        if (byId("faq-edit-dialog").open) byId("faq-edit-dialog").close();
        await render();
      }
      report(error.message || "Не удалось получить данные. Повторите действие.");
    } finally {
      ctl.disabled = false;
    }
  }

  const mail = createMail({api, requireExpert, run, status});
  const cards = createCards({api, requireExpert, run, status, openSource, refresh: () => render(),
    openInbox: (threadId) => open({threadId})});
  const insights = createInsights({api, requireExpert, run, openRegulation});

  async function render() {
    const ticket = ++generation;
    status();
    for (const control of document.querySelectorAll("[data-quality-tab]")) {
      control.setAttribute("aria-current", String(control.dataset.qualityTab === current));
    }
    byId("quality-title").textContent = titles[current];
    byId("quality-head-search").hidden = current !== "inbox";
    content.dataset.tab = current;
    content.setAttribute("aria-label", titles[current]);
    content.setAttribute("aria-busy", "true");
    content.replaceChildren(empty("Загрузка…"));
    try {
      const drawing = {faq: cards.render, inbox: mail.render, sources: insights.sources, analytics: insights.stats};
      const view = await drawing[current]();
      if (ticket === generation) content.replaceChildren(view);
    } catch (error) {
      if (ticket === generation) {
        content.replaceChildren(empty("Не удалось загрузить раздел."));
        status(error.message);
      }
    } finally {
      if (ticket === generation) content.removeAttribute("aria-busy");
    }
  }
  async function open({threadId} = {}) {
    if (threadId) {
      current = "inbox";
      mail.selectThread(threadId);
    }
    await render();
  }
  document.querySelectorAll("[data-quality-tab]").forEach((ctl) => {
    ctl.onclick = () => {
      mail.resetDraft();
      current = ctl.dataset.qualityTab;
      render();
      byId("quality-title").focus();
    };
  });
  byId("mail-search").oninput = () => mail.search();
  byId("quality-login").onclick = () => {
    if (!expert) return promptLogin();
    run(byId("quality-login"), async () => {
      await api("/v1/browser/mail/logout", {});
      mail.resetDraft();
      role(false);
      await render();
    });
  };
  const submitLogin = () => run(byId("expert-enter"), async () => {
    const data = await api("/v1/browser/mail/login", {
      login: byId("expert-user").value.trim(), password: byId("expert-password").value,
    });
    role(data.mail);
    const resume = afterLogin;
    login.close();
    byId("expert-password").value = "";
    // The pending action belongs to the current view. Refreshing here would
    // discard its controls (for example the group the expert wants to expand).
    if (resume) await resume();
  }, (message) => {
    byId("expert-error").textContent = message;
    byId("expert-error").hidden = false;
  });
  login.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing && event.target.tagName === "INPUT") {
      event.preventDefault();
      submitLogin();
    }
  });
  login.addEventListener("close", () => {
    afterLogin = undefined;
    byId("expert-password").value = "";
  });
  byId("expert-enter").onclick = submitLogin;
  for (const dialog of [login, byId("faq-edit-dialog"), byId("faq-thread-dialog")]) {
    dialog.querySelectorAll("[data-close-dialog]").forEach((ctl) => { ctl.onclick = () => dialog.close(); });
  }
  role(expert);
  return {open};
}
