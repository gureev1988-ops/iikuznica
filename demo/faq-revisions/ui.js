(() => {
  'use strict';

  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);

  function quote(clause, side = 'text') {
    let source = clause[side].trim();
    const prefix = clause.ref + '.';
    if (source.startsWith(prefix)) source = source.slice(prefix.length).trim();
    const html = escape(source);
    return clause.changed
      ? html.replace(/\b([57])(?=\s+рабоч)/, '<mark class="fq-value">$1</mark>')
      : html;
  }

  function tag(label, type = 'neutral') {
    return '<span class="fq-tag fq-tag-' + type + '">' + escape(label) + '</span>';
  }

  function ask(id) {
    return '<button type="button" class="fq-button" data-fq-ask="' + escape(id) + '">Спросить в чате</button>';
  }

  function citations(refs) {
    const prefix = refs.length === 1 ? 'Пункт ' : 'Пункты ';
    return escape(prefix + refs.join(' и ') + ' · ДБРЦТ');
  }

  function documentCard(state) {
    const terms = state.clauses.find(item => item.ref === '1.6');
    const contractor = state.clauses.find(item => item.ref === '1.7');
    const synthetic = state.synthetic;
    const documentText = synthetic
      ? '<div class="fq-diff"><div class="fq-version"><span class="fq-label">Было · исходная редакция</span><blockquote>' +
        quote(terms, 'originalText') + '</blockquote></div><div class="fq-version fq-version-new"><span class="fq-label">Стало · деморедакция</span><blockquote>' +
        quote(terms) + '</blockquote></div></div>'
      : '<blockquote class="fq-quote">' + quote(terms) + '</blockquote>';
    const action = synthetic
      ? '<p class="fq-status">Проверены связи двух ответов FAQ. Изменение затронуло один ответ.</p>'
      : '<button type="button" class="fq-button fq-primary" data-fq-action="import">Импортировать деморедакцию</button>';
    return '<section class="fq-card"><h3>Регламент</h3><p class="fq-doc-title">' + escape(state.documentTitle) + '</p>' +
      '<h4>Пункт 1.6 · Сроки оценки</h4>' + documentText +
      '<details class="fq-source"><summary>Пункт 1.7 · Участие подрядчика</summary><blockquote>' +
      quote(contractor) + '</blockquote></details><div class="fq-card-footer">' +
      '<p class="fq-note">Демонстрационное изменение — рекомендуемый срок точной оценки увеличен с 5 до 7 рабочих дней. Исходный файл компании сохранён.</p>' +
      action + '</div></section>';
  }

  function faqCard(state) {
    const affected = state.faqs.find(item => item.id === 'estimate');
    const control = state.faqs.find(item => item.id === 'questionnaire');
    const reviewing = affected.status === 'review';
    const done = state.synthetic && !reviewing;
    const status = reviewing ? tag('На проверке', 'warn') : tag(done ? 'Обновлён' : 'Утверждён', 'good');
    const main = reviewing
      ? '<p class="fq-note">Пункт 1.6 изменился. Прежний ответ временно снят с публикации.</p>' +
        '<details class="fq-old"><summary>Прежний ответ FAQ</summary><p>' + escape(affected.originalAnswer) + '</p></details>' +
        '<div class="fq-proposal"><span class="fq-label">Новая формулировка</span><p>' + escape(affected.draft) + '</p></div>'
      : '<p>' + escape(affected.answer) + '</p>';
    const approval = affected.approval;
    const signed = !reviewing && approval
      ? '<p class="fq-signature">' + escape(approval.actor) + ' · ' +
        escape(approval.date.split('-').reverse().join('.')) + ' · демоподпись</p>'
      : '';
    const publish = reviewing
      ? '<button type="button" class="fq-button fq-primary" data-fq-action="publish" data-fq-token="' + escape(affected.reviewToken) +
        '">Утвердить ответ</button>'
      : '';
    return '<section class="fq-card"><h3>Ответы FAQ</h3><article class="fq-entry"><div class="fq-entry-head"><h4>' +
      escape(affected.question) + '</h4>' + status + '</div><p class="fq-refs">' + citations(affected.refs) +
      '</p>' + main + signed + '<div class="fq-actions">' + publish + ask(affected.id) + '</div></article>' +
      '<details class="fq-control"><summary><span>' + escape(control.question) + '</span>' + tag(state.synthetic ? 'Без изменений' : 'Утверждён') +
      '</summary><p class="fq-refs">' + citations(control.refs) + '</p><p>' + escape(control.answer) + '</p>' +
      '<div class="fq-actions">' + ask(control.id) + '</div></details></section>';
  }

  class FaqRevisionDemo extends HTMLElement {
    constructor() {
      super();
      this.model = window.FaqRevisionDemoModel.createModel();
      this.addEventListener('click', event => this.handleClick(event));
    }

    connectedCallback() {
      if (this.querySelector('[data-fq-desk]')) return;
      this.innerHTML = '<div class="fq-frame"><header class="fq-head"><div><h2 tabindex="-1">Проверка ответов FAQ</h2>' +
        '<p>Отдел качества · Проектная деятельность ДБРЦТ</p></div><button type="button" class="fq-button" data-fq-action="reset" hidden>' +
        'Начать заново</button></header><p class="fq-sr" role="status" aria-live="polite"></p>' +
        '<div class="fq-grid" data-fq-desk></div></div>';
      this.render();
    }

    render() {
      const state = this.model.state();
      this.querySelector('[data-fq-desk]').innerHTML = documentCard(state) + faqCard(state);
      this.querySelector('[data-fq-action="reset"]').hidden = !state.synthetic;
    }

    handleClick(event) {
      const button = event.target.closest('button');
      if (!button || !this.contains(button)) return;
      const id = button.dataset.fqAsk;
      if (id) {
        this.dispatchEvent(new CustomEvent('faq-ask', {bubbles: true, detail: {id}}));
        return;
      }
      let notice;
      let focus;
      switch (button.dataset.fqAction) {
        case 'import':
          this.model.importRevision();
          notice = 'Демонстрационная редакция загружена. Один ответ требует проверки.';
          focus = '[data-fq-action="publish"]';
          break;
        case 'publish':
          this.model.publish('estimate', button.dataset.fqToken);
          notice = 'Отдел качества утвердил обновлённый ответ. Его можно получить в чате.';
          focus = '[data-fq-ask="estimate"]';
          break;
        case 'reset':
          this.model.reset();
          notice = 'Исходные регламент и ответы восстановлены.';
          focus = '[data-fq-action="import"]';
          break;
        default:
          return;
      }
      this.render();
      this.querySelector('[role="status"]').textContent = notice;
      this.querySelector(focus)?.focus({preventScroll: false});
      this.dispatchEvent(new CustomEvent('faq-change', {bubbles: true, detail: {state: this.model.state()}}));
    }
  }

  if (!customElements.get('faq-revision-demo')) customElements.define('faq-revision-demo', FaqRevisionDemo);
})();
