(function (root, build) {
  const api = build();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.FaqRevisionDemoModel = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  const documentTitle = 'Проектная деятельность ДБРЦТ';
  const pageId = 'DEMO-DB';
  const scope = 'ДБРЦТ · пресейл';
  const actor = 'Уполномоченный сотрудник отдела качества';
  const date = '26.11.2026';
  const channelAnswer = 'Отметку о передаче оценки заказчику внесите в поле «Передача оценки» в карточке пресейла.';
  const channelQuestion = 'Где зафиксировать передачу оценки заказчику?';
  const receiptQuestion = 'Что указать в теме письма отделу качества при запросе подтверждения получения оценки?';
  const budgetQuestion = 'Как обозначить бюджетную оценку в письме и какой срок указать?';
  const receiptAnswer = 'В теме письма укажите «Подтверждение получения оценки» и номер заявки на пресейл.';

  // The original three clauses are from the synthetic hackathon corpus. Page history,
  // the added clause and all FAQ/mail examples are a read-only demonstration fixture.
  const original = {
    '1.1.1': {
      text: '1.1.1. При отсутствии в Заявке на пресейл квалификационной анкеты Заявка будет в ожидании информации со стороны Архитектора до её предоставления и не будет взята в работу.',
      facts: { withoutQuestionnaire: 'wait' }
    },
    '1.6': {
      text: '1.6. Рекомендуемые сроки выдачи:\nдля точных оценок – 5 рабочих дней;\nдля бюджетных оценок – 3 рабочих дня.',
      facts: { exactDays: 5, budgetDays: 3 }
    },
    '1.7': {
      text: '1.7. В случае подключения к оценке Подрядчика необходимо дополнительно учитывать SLA на поиск и согласование подрядчика.',
      facts: { contractorSla: 'search-and-agree' }
    }
  };
  const sourcePages = {
    v1: original,
    v2: {
      ...original,
      '1.6': { text: original['1.6'].text.replace('5 рабочих дней', '7 рабочих дней'), facts: { exactDays: 7, budgetDays: 3 } },
      'Д.1': { text: 'Д.1. ' + channelAnswer, facts: { deliveryRecord: 'presale-card' } }
    }
  };
  const history = [{
    pageId, from: 'v1', to: 'v2', actor: 'Владелец регламента ДБРЦТ (демо)', date,
    summary: 'Изменён срок точной оценки. Добавлен порядок фиксации передачи оценки.'
  }];
  const templates = [
    {
      id: 'estimate', question: 'Какие сроки рекомендует ДБРЦТ для бюджетной и точной оценки и что учесть при подключении подрядчика?',
      answer: 'Рекомендуемые сроки выдачи оценки в ДБРЦТ — 3 рабочих дня для бюджетной и 5 рабочих дней для точной. При подключении подрядчика дополнительно учитывай SLA на его поиск и согласование.',
      facts: { exactDays: 5, budgetDays: 3, contractorSla: 'search-and-agree' }
    },
    { id: 'channel', question: channelQuestion, answer: channelAnswer, facts: { deliveryRecord: 'presale-card' } },
    {
      id: 'questionnaire', question: 'Возьмёт ли ДБРЦТ заявку на пресейл в работу без квалификационной анкеты?',
      answer: 'Без квалификационной анкеты заявку на пресейл в работу не возьмут. Она останется в ожидании информации до предоставления анкеты.',
      facts: { withoutQuestionnaire: 'wait' }
    },
    { id: 'receipt', question: receiptQuestion, answer: null, facts: { receiptSubject: 'receipt-and-ticket' } },
    { id: 'budget-tag', question: budgetQuestion, answer: null, facts: { budgetDays: 3, budgetTag: 'budget' } }
  ];
  const messages = [
    {
      id: 'deadline', faqId: 'estimate', scope, actor, subject: 'Срок точной оценки',
      question: 'Сколько рабочих дней ДБРЦТ рекомендует для точной оценки без подрядчика?',
      body: 'Для точной оценки без подрядчика рекомендуем 2 рабочих дня.', facts: { exactDays: 2 },
      correctable: true
    },
    {
      id: 'receipt', faqId: 'receipt', scope, actor, subject: 'Тема письма о получении оценки',
      question: receiptQuestion, body: receiptAnswer, facts: { receiptSubject: 'receipt-and-ticket' }
    },
    {
      id: 'ambiguous', faqId: 'budget-tag', scope: null, actor, subject: 'Три дня на оценку',
      question: 'Какой срок и пометку указать в письме об оценке?',
      body: 'Рекомендуем три рабочих дня. В теме ставим пометку «Бюджет».',
      facts: { budgetDays: 3, exactDays: 3, budgetTag: 'budget' },
      clarification: 'Уточнение автора письма. Имеется в виду только бюджетная оценка без подрядчика в ДБРЦТ. Пометку «Бюджет» ставим в теме письма.',
      corrected: {
        scope, body: 'Для бюджетной оценки без подрядчика ДБРЦТ рекомендует 3 рабочих дня. В теме письма укажите «Бюджет».',
        facts: { budgetDays: 3, budgetTag: 'budget' }
      }
    }
  ];

  // The fixture contains structured facts and scope. No probabilistic score or live
  // classification is simulated. A gap is usable only when the scope is known.
  function compare(facts, candidateScope, page) {
    const keys = Object.keys(facts);
    const refs = [], matches = new Set(), conflicts = new Set();
    for (const [ref, clause] of Object.entries(page)) {
      const related = keys.filter(key => Object.hasOwn(clause.facts, key));
      if (related.length) refs.push(ref);
      for (const key of related) {
        (clause.facts[key] === facts[key] ? matches : conflicts).add(key);
      }
    }
    let kind = 'gap';
    if (candidateScope !== scope) kind = 'uncertain';
    else if (conflicts.size) kind = 'conflict';
    else if (keys.length && matches.size === keys.length) kind = 'covered';
    return { kind, refs };
  }

  let sequence = 0;
  function createModel() {
    let revision, event, changedRefs, faqs, mails, log;
    const token = () => 'decision-' + (++sequence);
    const currentPage = () => sourcePages[revision];
    const note = text => log.push({ text });
    function citations(refs) {
      return refs.map(ref => ({
        docId: 'DB', documentTitle, pageId, ref, text: currentPage()[ref].text, revision,
        synthetic: revision === 'v2', changed: changedRefs.includes(ref)
      }));
    }
    function assessment(template) {
      return compare(template.facts, template.scope, currentPage());
    }
    function correction(template) {
      if (template.corrected) return template.corrected;
      if (!template.correctable) return null;
      const currentDays = currentPage()['1.6'].facts.exactDays;
      return {
        scope, body: 'Для точной оценки без подрядчика рекомендуем ' + currentDays + ' рабочих дней.',
        facts: { exactDays: currentDays }
      };
    }
    function reset() {
      revision = 'v1';
      event = null;
      changedRefs = [];
      faqs = new Map(templates.map(template => [template.id, {
        status: template.answer ? 'published' : 'absent', answer: template.answer, version: template.answer ? revision : null,
        reason: '', mailId: null, reviewToken: null, deferred: false
      }]));
      mails = new Map(messages.map(template => [template.id, {
        status: 'unread', reason: '', reviewToken: null, checkedVersion: null, refs: [], reviewedBody: null
      }]));
      log = [];
      return state();
    }
    function state() {
      return {
        revision, docId: 'DB', documentTitle, synthetic: revision === 'v2', changedRefs: [...changedRefs], event: event && { ...event },
        clauses: Object.entries(currentPage()).map(([ref, clause]) => ({
          ref, text: clause.text, originalText: original[ref]?.text || '', changed: changedRefs.includes(ref)
        })),
        faqs: templates.map(template => ({
          id: template.id, question: template.question, ...faqs.get(template.id), refs: compare(template.facts, scope, currentPage()).refs
        })),
        mails: messages.map(template => {
          const record = mails.get(template.id);
          return {
            id: template.id, faqId: template.faqId, actor: template.actor, question: template.question, subject: template.subject,
            body: template.body, scope: template.scope, status: record.status, reason: record.reason,
            reviewToken: record.reviewToken, checkedVersion: record.checkedVersion, reviewedBody: record.reviewedBody,
            canRevise: record.status === 'review' && !!correction(template),
            clarification: record.status === 'unread' ? null : template.clarification || null,
            correctedBody: record.status === 'review' ? correction(template)?.body || null : null,
            evidence: record.refs.map(ref => ({
              ref, text: sourcePages[record.checkedVersion][ref].text, revision: record.checkedVersion
            }))
          };
        }),
        log: log.map(line => ({ ...line }))
      };
    }

    function publishMail(template, record, body) {
      record.status = 'published';
      Object.assign(faqs.get(template.faqId), {
        status: 'published', answer: body, version: revision, reason: record.reason, mailId: template.id, reviewToken: null, deferred: false
      });
    }
    function checkMail(template) {
      const record = mails.get(template.id);
      const { kind, refs } = assessment(template);
      record.checkedVersion = revision;
      record.refs = refs;
      record.reviewToken = null;
      if (kind === 'conflict' || kind === 'uncertain') {
        record.status = 'review';
        record.reviewToken = token();
        record.reason = kind === 'uncertain'
          ? 'Неясны вид оценки и область применения ответа. Нужна модерация.'
          : 'Рекомендованный в письме срок расходится с действующим п. ' + refs.join(', ') + '.';
      } else if (kind === 'covered') {
        record.status = 'covered';
        record.reason = 'Действующий регламент уже полностью отвечает на вопрос.';
      } else {
        // All senders in the demo inbox are authorized quality officers.
        record.reason = 'В применимых пунктах демонабора нет требования к теме письма. Разъяснение опубликовано в FAQ.';
        publishMail(template, record, template.body);
      }
      note('Письмо «' + template.subject + '». ' + record.reason);
    }
    function syncConfluence() {
      // Read the next externally authored event after our checkpoint. There is no
      // operation for authoring a page or creating a Confluence version in the demo.
      const next = history.find(entry => entry.from === revision);
      if (!next) return state();
      const old = currentPage();
      revision = next.to;
      event = next;
      const page = currentPage();
      changedRefs = [...new Set([...Object.keys(old), ...Object.keys(page)])].filter(ref => old[ref]?.text !== page[ref]?.text);
      note('Прочитана история страницы ' + pageId + ' в Confluence. ' + next.from + ' → ' + next.to + '.');
      for (const template of templates) {
        const record = faqs.get(template.id);
        if (!['published', 'review'].includes(record.status)) continue;
        const before = compare(template.facts, scope, old);
        const after = compare(template.facts, scope, page);
        if (![...before.refs, ...after.refs].some(ref => changedRefs.includes(ref))) continue;
        if (after.kind === 'conflict') {
          record.status = 'review';
          record.reviewToken = token();
          record.deferred = false;
          record.reason = 'Ответ FAQ расходится с п. ' + after.refs.filter(ref => changedRefs.includes(ref)).join(', ') + '. Удаление подтверждает отдел качества.';
        } else if (after.kind === 'covered') {
          record.status = 'archived';
          record.reviewToken = null;
          record.reason = 'Регламент полностью отвечает на вопрос. FAQ автоматически снят с публикации.';
        } else {
          // Partial coverage alone does not justify an automatic archive.
          continue;
        }
        note(template.question + ' ' + record.reason);
      }
      for (const template of messages) {
        if (mails.get(template.id).status === 'review') checkMail(template);
      }
      return state();
    }
    function receiveMail(id) {
      const template = messages.find(message => message.id === id);
      if (template && mails.get(id).status === 'unread') checkMail(template);
      return state();
    }
    function decideFaq(id, expectedToken, action) {
      const record = faqs.get(id);
      if (!record || record.status !== 'review' || !expectedToken || record.reviewToken !== expectedToken) return state();
      if (action === 'remove') {
        record.status = 'archived';
        record.reviewToken = null;
        record.reason = 'Отдел качества подтвердил удаление из активного FAQ. Запись сохранена в истории.';
      } else if (action === 'defer' && !record.deferred) {
        record.deferred = true;
        record.reviewToken = token();
        record.reason = 'Решение отложено отделом качества. Ответ скрыт из FAQ до разрешения противоречия.';
      } else return state();
      note(templates.find(template => template.id === id).question + ' ' + record.reason);
      return state();
    }
    function decideMail(id, expectedToken, action) {
      const template = messages.find(message => message.id === id);
      const record = mails.get(id);
      if (!record || record.status !== 'review' || !expectedToken || record.reviewToken !== expectedToken || record.checkedVersion !== revision) return state();
      if (action === 'reject') {
        record.status = 'rejected';
        record.reason = 'Ответ не принят в FAQ. Оригинал письма сохранён.';
      } else if (action === 'revise') {
        const proposed = correction(template);
        if (!proposed) return state();
        const check = compare(proposed.facts, proposed.scope, currentPage());
        if (!['covered', 'gap'].includes(check.kind)) return state();
        record.refs = check.refs;
        record.reviewedBody = proposed.body;
        if (check.kind === 'covered') {
          record.status = 'covered';
          record.reason = 'Исправление проверено по действующей версии. Регламент полностью отвечает на вопрос, дубликат в FAQ не нужен.';
        } else {
          record.reason = 'Автор подтвердил вид оценки. Срок сверён с регламентом, пометка в теме письма дополняет его. Отдел качества принял уточнённый FAQ.';
          publishMail(template, record, proposed.body);
        }
      } else return state();
      record.reviewToken = null;
      note('Письмо «' + template.subject + '». ' + record.reason);
      return state();
    }
    function query(id) {
      const template = templates.find(entry => entry.id === id);
      if (!template) return null;
      const record = faqs.get(id);
      const refs = compare(template.facts, scope, currentPage()).refs;
      const sources = citations(refs);
      const published = record.status === 'published';
      const hasRegulationAnswer = sources.length > 0 && record.status !== 'absent';
      const answerKind = published ? 'faq' : hasRegulationAnswer ? 'regulation' : 'none';
      let answer = 'По этому вопросу пока нет подтверждённого ответа.';
      if (published) answer = record.answer;
      else if (hasRegulationAnswer) answer = sources.map(source => source.text.slice(source.ref.length + 2)).join('\n\n');
      let approval = null;
      if (published) {
        approval = { actor, date, simulated: true };
        const sourceMail = messages.find(message => message.id === record.mailId);
        if (sourceMail) Object.assign(approval, {
          mailId: sourceMail.id, subject: sourceMail.subject, actor: sourceMail.actor,
          verifiedText: sourceMail.clarification || sourceMail.body
        });
      }
      return {
        id, question: template.question, status: record.status, answerKind, answer, refs, sources, documentTitle,
        version: published ? record.version : hasRegulationAnswer ? revision : null, currentRevision: revision, synthetic: revision === 'v2',
        approval
      };
    }
    reset();
    return { state, query, syncConfluence, receiveMail, decideFaq, decideMail, reset };
  }
  return { createModel };
});
