(() => {
  'use strict';

  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);

  const labels = {
    published: ['В FAQ', 'good'], review: ['На модерации', 'warn'],
    archived: ['В архиве', ''], unread: ['Новое', ''],
    rejected: ['Отклонено', ''], covered: ['Есть в регламенте', 'good']
  };

  function tag(status) {
    const [text, tone] = labels[status];
    return '<span class="fq-tag fq-' + tone + '">' + text + '</span>';
  }

  function action(key, label, {primary = false, id = '', token = ''} = {}) {
    return '<button type="button" class="fq-button' + (primary ? ' fq-primary' : '') +
      '" data-fq-action="' + key + '" data-fq-id="' + escape(id) + '" data-fq-token="' + escape(token) + '">' +
      escape(label) + '</button>';
  }

  function ask(id) {
    return '<button type="button" class="fq-button" data-fq-ask="' + escape(id) + '">Спросить в чате</button>';
  }

  function quote(label, text) {
    return '<div class="fq-quote"><span class="fq-label">' + escape(label) + '</span><blockquote>' +
      escape(text) + '</blockquote></div>';
  }

  function details(label, html) {
    return '<details class="fq-source"><summary>' + escape(label) + '</summary>' + html + '</details>';
  }

  function sourceCard(state) {
    const event = state.event;
    const version = event ? 'Получена новая версия' : 'Последняя проверка · версия 1';
    const changes = state.clauses.filter(clause => clause.changed).map(clause => {
      const previous = clause.originalText ? quote('До изменения', clause.originalText) : '';
      return details('п. ' + clause.ref + (previous ? ' · Изменение текста' : ' · Новый пункт'),
        previous + quote('Действующий текст', clause.text));
    }).join('');
    const report = event
      ? '<h4>' + escape(event.summary) + '</h4><p class="fq-meta">Страница ' + escape(event.pageId) +
        ' · ' + escape(event.from) + ' → ' + escape(event.to) + ' · ' + escape(event.actor) +
        ' · ' + escape(event.date) + '</p>' + changes
      : '<p>В журнале Confluence доступна следующая редакция. Проверка загрузит её и найдёт затронутые ответы FAQ.</p>';
    return '<section class="fq-card"><h3>Журнал Confluence</h3><p class="fq-meta">' +
      escape(state.documentTitle) + '</p><p class="fq-note">' + version + '</p>' +
      '<div class="fq-body">' + report + '</div><div class="fq-actions">' +
      action('sync', event ? 'Проверить ещё раз' : 'Проверить изменения в Confluence', {primary: !event}) +
      '</div></section>';
  }

  function faqRow(faq, state, confirmation) {
    const reviewing = faq.status === 'review';
    const rowTitle = '<div class="fq-title"><h4>' + escape(faq.question) + '</h4>' + tag(faq.status) + '</div>';
    const why = faq.reason ? '<p class="fq-note">' + escape(faq.reason) + '</p>' : '';
    const refs = state.clauses.filter(clause => faq.refs.includes(clause.ref));
    const compare = reviewing
      ? quote('Прежний ответ FAQ', faq.answer) + refs.filter(item => item.changed).map(clause =>
        quote('Действующий регламент · п. ' + clause.ref, clause.text)).join('')
      : details(faq.status === 'archived' ? 'Архивная запись' : 'Ответ FAQ', quote('Отдел качества', faq.answer));
    const opts = {id: faq.id, token: faq.reviewToken};
    let controls = ask(faq.id);
    if (reviewing && confirmation === faq.reviewToken) {
      controls = '<div class="fq-decision"><h4>Удалить разъяснение из активного FAQ?</h4>' +
        '<p class="fq-note">В архиве сохранится прежний ответ. Сотрудник получит действующий пункт регламента.</p>' +
        '<div class="fq-actions">' + action('remove', 'Подтвердить удаление', {...opts, primary: true}) +
        action('cancel-remove', 'Отмена', opts) + '</div></div>';
    } else if (reviewing) {
      controls = action('confirm-remove', 'Удалить из FAQ…', {...opts, primary: true}) +
        (faq.deferred ? '' : action('defer', 'Отложить решение', opts)) + controls;
    }
    return '<article class="fq-row" data-fq-row="' + escape(faq.id) + '">' +
      rowTitle + why + compare + '<div class="fq-actions">' + controls + '</div></article>';
  }

  function faqCard(state, confirmation) {
    const group = (status, title) => {
      const items = state.faqs.filter(faq => faq.status === status);
      if (!items.length) return '';
      const contents = items.map(faq => faqRow(faq, state, confirmation)).join('');
      if (status === 'published' && state.event) return details(title + ' · ' + items.length, contents);
      return '<section class="fq-group"><h4 class="fq-group-title">' + title + ' · ' +
        items.length + '</h4>' + contents + '</section>';
    };
    return '<section class="fq-card"><h3>Проверка записей FAQ</h3><div class="fq-body">' +
      group('review', 'Требуют решения') + group('archived', 'Сняты с публикации') +
      group('published', 'Активные ответы') + '</div></section>';
  }

  function inbox(state, selectedId) {
    const items = state.mails.map(mail =>
      '<button type="button" class="fq-choice" data-fq-mail="' + escape(mail.id) + '" aria-pressed="' +
      (mail.id === selectedId) + '">' + escape(mail.subject) + ' ' + tag(mail.status) + '</button>').join('');
    return '<section class="fq-card"><h3>Письма отдела качества</h3><p class="fq-note">Ответ уполномоченного сотрудника проходит сверку перед добавлением в FAQ.</p><div class="fq-body">' +
      items + '</div></section>';
  }

  function mailCard(mail) {
    const opts = {id: mail.id, token: mail.reviewToken};
    const unread = mail.status === 'unread';
    const reviewing = mail.status === 'review';
    const evidence = unread ? '' : mail.evidence.map(clause => quote('Регламент при сверке · ' + clause.revision + ' · п. ' + clause.ref, clause.text)).join('');
    const reason = unread ? '' : '<div class="fq-body"><h4>Результат сверки</h4><p class="fq-note">' +
      escape(mail.reason) + '</p>' + evidence + '</div>';
    const clarification = reviewing && mail.clarification ? quote('Уточнение от автора', mail.clarification) : '';
    const correction = reviewing && mail.canRevise
      ? clarification + quote('Формулировка для повторной сверки', mail.correctedBody)
      : mail.reviewedBody ? quote('Проверенная формулировка', mail.reviewedBody) : '';
    const buttons = [];
    if (unread) buttons.push(action('receive', 'Проверить письмо', {...opts, primary: true}));
    if (reviewing) {
      if (mail.canRevise) buttons.push(action('revise', 'Проверить и принять уточнение', opts));
      buttons.push(action('reject', 'Отклонить письмо', {...opts, primary: true}));
    }
    if (!unread && mail.faqId) buttons.push(ask(mail.faqId));
    return '<article class="fq-card" data-fq-message="' + escape(mail.id) + '"><div class="fq-title"><h3>' +
      escape(mail.subject) + '</h3>' + tag(mail.status) + '</div><p class="fq-meta">Отправитель · ' + escape(mail.actor) +
      '</p><div class="fq-body"><h4>' + escape(mail.question) + '</h4>' + quote('Исходное письмо', mail.body) +
      '</div>' + reason + correction + '<div class="fq-actions">' + buttons.join('') + '</div></article>';
  }

  class FaqRevisionDemo extends HTMLElement {
    constructor() {
      super();
      this.model = window.FaqRevisionDemoModel.createModel();
      this.tab = 'source';
      this.mailId = 'deadline';
      this.confirmation = null;
      this.addEventListener('click', event => this.handleClick(event));
    }

    connectedCallback() {
      if (this.querySelector('[data-fq-desk]')) return;
      this.innerHTML = '<div class="fq-frame"><header class="fq-head"><div><h2 tabindex="-1">Контроль FAQ</h2>' +
        '<p class="fq-note">Демоданные · Снимок истории Confluence и учебные письма отдела качества</p></div>' +
        action('reset', 'Начать заново') + '</header><nav class="fq-nav" aria-label="Проверки FAQ">' +
        '<button class="fq-tab" type="button" data-fq-tab="source">Изменения регламентов</button>' +
        '<button class="fq-tab" type="button" data-fq-tab="mail">Входящие письма</button></nav>' +
        '<p class="fq-notice" role="status" aria-live="polite"></p><div class="fq-grid" data-fq-desk></div>' +
        '<div class="fq-log" data-fq-log></div></div>';
      this.render();
    }

    render(message = '') {
      const state = this.model.state();
      const desk = this.querySelector('[data-fq-desk]');
      for (const item of this.querySelectorAll('[data-fq-tab]')) {
        item.setAttribute('aria-pressed', String(item.dataset.fqTab === this.tab));
      }
      if (this.tab === 'source') {
        desk.innerHTML = sourceCard(state) + faqCard(state, this.confirmation);
      } else {
        const selected = state.mails.find(mail => mail.id === this.mailId) || state.mails[0];
        desk.innerHTML = inbox(state, selected.id) + mailCard(selected);
      }
      const log = this.querySelector('[data-fq-log]');
      log.innerHTML = state.log.length ? details('Журнал решений · ' + state.log.length, '<ul>' +
        state.log.map(entry => '<li>' + escape(entry.text) + '</li>').join('') + '</ul>') : '';
      this.querySelector('[role="status"]').textContent = message;
    }

    handleClick(event) {
      const button = event.target.closest('button');
      if (!button || !this.contains(button)) return;
      const data = button.dataset;
      if (data.fqAsk) {
        this.dispatchEvent(new CustomEvent('faq-ask', {bubbles: true, detail: {id: data.fqAsk}}));
        return;
      }
      if (data.fqTab || data.fqMail) {
        this.tab = data.fqTab || 'mail';
        if (data.fqMail) this.mailId = data.fqMail;
        this.confirmation = null;
        this.render();
        if (data.fqMail) this.querySelector('[data-fq-mail][aria-pressed="true"]')?.focus();
        return;
      }
      const id = data.fqId;
      const token = data.fqToken;
      let message = '';
      let focus;
      let changed = true;
      switch (data.fqAction) {
        case 'sync': {
          const alreadySynced = Boolean(this.model.state().event);
          this.model.syncConfluence();
          message = alreadySynced ? 'Новых версий в журнале нет.' : 'Изменения обработаны. Решения по затронутым вопросам показаны в FAQ.';
          focus = '[data-fq-action="confirm-remove"]';
          break;
        }
        case 'confirm-remove':
          this.confirmation = token;
          changed = false;
          focus = '[data-fq-action="remove"]';
          break;
        case 'cancel-remove':
          this.confirmation = null;
          changed = false;
          focus = '[data-fq-action="confirm-remove"]';
          message = 'Удаление отменено. Спорный ответ остаётся на проверке.';
          break;
        case 'remove':
        case 'defer':
          this.confirmation = null;
          this.model.decideFaq(id, token, data.fqAction);
          focus = '[data-fq-ask="' + id + '"]';
          break;
        case 'receive':
          this.model.receiveMail(id);
          focus = '[data-fq-message] .fq-actions button';
          break;
        case 'revise':
        case 'reject':
          this.model.decideMail(id, token, data.fqAction);
          focus = '[data-fq-message] .fq-actions button';
          break;
        case 'reset':
          this.model.reset();
          this.confirmation = null;
          this.tab = 'source';
          this.mailId = 'deadline';
          message = 'Демонстрация начата заново.';
          focus = '[data-fq-action="sync"]';
          break;
        default:
          return;
      }
      const state = this.model.state();
      if (!message && changed) message = state.log.at(-1)?.text || '';
      this.render(message);
      (this.querySelector(focus) || this.querySelector('[data-fq-tab][aria-pressed="true"]')).focus();
      if (changed) this.dispatchEvent(new CustomEvent('faq-change', {bubbles: true, detail: {state}}));
    }
  }

  if (!customElements.get('faq-revision-demo')) customElements.define('faq-revision-demo', FaqRevisionDemo);
})();
