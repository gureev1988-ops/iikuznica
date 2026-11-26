(function (root, build) {
  const api = build();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.FaqRevisionDemoModel = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  const documentTitle = 'Проектная деятельность ДБРЦТ';
  const pageId = 'DEMO-DB';
  const scope = 'ДБРЦТ · пресейл';
  const actor = 'Уполномоченный эксперт службы качества';
  const date = '26.11.2026';
  const channelAnswer = 'Отметку о передаче оценки заказчику внесите в поле «Передача оценки» в карточке пресейла.';
  const channelQuestion = 'Где зафиксировать передачу оценки заказчику?';
  const receiptQuestion = 'Что указать в теме письма отделу качества при запросе подтверждения получения оценки?';
  const budgetQuestion = 'Как обозначить бюджетную оценку в письме и какой срок указать?';
  const receiptAnswer = 'В теме письма укажите «Подтверждение получения оценки» и номер заявки на пресейл.';
  const estimateQuestion = 'Какие сроки рекомендует ДБРЦТ для бюджетной и точной оценки и что учесть при подключении подрядчика?';

  // All mail, the page history and the second revision are demonstration fixtures.
  // The three original clauses are from the hackathon corpus.
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
      id: 'estimate', question: estimateQuestion, subject: 'Разъяснение по срокам оценки ДБРЦТ',
      answer: 'Рекомендуемые сроки выдачи оценки в ДБРЦТ — 3 рабочих дня для бюджетной и 5 рабочих дней для точной. При подключении подрядчика дополнительно учитывай SLA на его поиск и согласование.',
      facts: { exactDays: 5, budgetDays: 3, contractorSla: 'search-and-agree' }
    },
    { id: 'channel', question: channelQuestion, subject: 'Фиксация передачи оценки', answer: channelAnswer, facts: { deliveryRecord: 'presale-card' } },
    {
      id: 'questionnaire', question: 'Возьмёт ли ДБРЦТ заявку на пресейл в работу без квалификационной анкеты?',
      subject: 'Пресейл без квалификационной анкеты',
      answer: 'Без квалификационной анкеты заявку на пресейл в работу не возьмут. Она останется в ожидании информации до предоставления анкеты.',
      facts: { withoutQuestionnaire: 'wait' }
    },
    { id: 'receipt', question: receiptQuestion, facts: { receiptSubject: 'receipt-and-ticket' } },
    { id: 'budget-tag', question: budgetQuestion, facts: { budgetDays: 3, budgetTag: 'budget' } }
  ];
  const messages = [
    {
      id: 'deadline', faqId: 'estimate', verified: true, scope, actor, subject: 'Срок точной оценки',
      question: estimateQuestion, supersedes: 'estimate-initial',
      body: 'Уточняем нашу прежнюю рекомендацию для ДБРЦТ. На точную оценку рекомендуем 2 рабочих дня, на бюджетную — 3 рабочих дня. При привлечении подрядчика дополнительно учитывайте SLA на его поиск и согласование.',
      facts: { exactDays: 2, budgetDays: 3, contractorSla: 'search-and-agree' }
    },
    {
      id: 'receipt', faqId: 'receipt', verified: true, scope, actor, subject: 'Тема письма о получении оценки',
      question: receiptQuestion, body: receiptAnswer, facts: { receiptSubject: 'receipt-and-ticket' }
    },
    {
      id: 'ambiguous', faqId: 'budget-tag', verified: true, scope: null, actor, subject: 'Три дня на оценку',
      question: 'Какой срок и пометку указать в письме об оценке?',
      body: 'Рекомендуем три рабочих дня. В теме ставим пометку «Бюджет».',
      facts: { budgetDays: 3, exactDays: 3, budgetTag: 'budget' },
      clarification: 'Имеется в виду только бюджетная оценка без подрядчика в ДБРЦТ. Рекомендуем 3 рабочих дня. В теме письма укажите «Бюджет».',
      corrected: {
        scope, body: 'Для бюджетной оценки без подрядчика ДБРЦТ рекомендует 3 рабочих дня. В теме письма укажите «Бюджет».',
        facts: { budgetDays: 3, budgetTag: 'budget' }
      }
    },
    {
      id: 'unverified', faqId: 'estimate', verified: false, scope, actor: 'Автор пересланной копии',
      question: estimateQuestion, subject: 'Пересланная рекомендация', body: 'Точная оценка занимает 1 рабочий день.',
      facts: { exactDays: 1 }
    },
    {
      id: 'other-scope', faqId: 'estimate', verified: true, scope: 'Дирекция ИБ · пресейл', actor,
      question: estimateQuestion, subject: 'Срок в другой дирекции',
      body: 'В дирекции ИБ рекомендуем 4 рабочих дня на точную оценку.', facts: { exactDays: 4 }
    }
  ];

  // Compare already annotated claims. A different value implies a discrepancy
  // only for the same question and scope. Source authority is a separate check.
  function compare(facts, candidateScope, page) {
    const keys = Object.keys(facts);
    const refs = [], differences = [], matches = new Set();
    if (candidateScope !== scope) return { kind: 'out-of-scope', refs, differences, addressesAll: false };
    for (const [ref, clause] of Object.entries(page)) {
      const related = keys.filter(key => Object.hasOwn(clause.facts, key));
      if (related.length) refs.push(ref);
      for (const key of related) {
        if (clause.facts[key] === facts[key]) matches.add(key);
        else differences.push({ ref, key, expert: facts[key], regulation: clause.facts[key] });
      }
    }
    const covered = keys.length > 0 && matches.size === keys.length;
    const addressed = new Set([...matches, ...differences.map(item => item.key)]);
    return { kind: differences.length ? 'conflict' : covered ? 'covered' : 'gap', refs, differences,
      addressesAll: keys.length > 0 && addressed.size === keys.length };
  }

  let sequence = 0;
  // Tests can supply another read-only journal or annotated inbox.
  function createModel({ inbox = messages, pages = sourcePages, events = history } = {}) {
    let revision, event, changedRefs, faqs, mails, log, session, withdrawals;
    const token = () => 'decision-' + (++sequence);
    const note = text => log.push({ text });
    function evaluation(facts, appliesTo, version) {
      const check = compare(facts, appliesTo, pages[version]);
      const refs = [...new Set(check.differences.map(difference => difference.ref))];
      const sourceDate = events.find(item => item.to === version)?.date || null;
      const description = check.differences.map(({key, ref, expert, regulation}) => {
        if (key === 'exactDays' || key === 'budgetDays') {
          const kind = key === 'exactDays' ? 'точной' : 'бюджетной';
          return 'Для ' + kind + ' оценки эксперт рекомендует ' + expert + ' раб. дн. В п. ' + ref
            + ' версии ' + version + ' указано ' + regulation + ' раб. дн.';
        }
        return 'Разъяснение эксперта расходится с п. ' + ref + ' версии ' + version + '.';
      });
      const discrepancy = refs.length ? {
        refs, revision: version, note: description.join(' '), differences: check.differences
      } : null;
      const sources = check.refs.map(ref => ({
        docId: 'DB', documentTitle, pageId, ref, revision: version, date: sourceDate,
        text: pages[version][ref].text, role: refs.includes(ref) ? 'discrepancy' : 'support',
        synthetic: version !== 'v1', changed: version === revision && changedRefs.includes(ref)
      }));
      return { kind: check.kind, refs: check.refs, addressesAll: check.addressesAll, discrepancy, sources };
    }

    function reset() {
      session = token();
      withdrawals = new Set();
      revision = 'v1';
      event = null;
      changedRefs = [];
      faqs = new Map(templates.map(template => [template.id, {
        status: template.answer ? 'published' : 'absent', answer: template.answer || null, version: template.answer ? revision : null,
        reason: '', facts: template.facts,
        approval: template.answer ? { actor, date, scope, simulated: true, mailId: template.id + '-initial',
          subject: template.subject, verifiedText: template.answer } : null
      }]));
      mails = new Map(inbox.map(template => [template.id, {
        status: 'unread', reason: '', reviewToken: null, checkedVersion: null, reviewedBody: null
      }]));
      log = [];
      return state();
    }

    function assessMail(template, record) {
      const candidate = record.reviewedBody ? template.corrected : template;
      return evaluation(candidate.facts, candidate.scope, record.checkedVersion);
    }
    function activeApproval(id) {
      const record = faqs.get(id);
      return record.status === 'withdrawn' ? null : record.approval;
    }
    function withdrawKey(templateId, record, discrepancy) {
      return record.status === 'published' && discrepancy
        ? [session, templateId, record.approval.mailId, revision].join(':') : null;
    }
    function mailView(template) {
      const record = mails.get(template.id);
      const checked = record.checkedVersion && assessMail(template, record);
      const discrepancy = template.verified && checked ? checked.discrepancy : null;
      const candidate = record.reviewedBody ? template.corrected : template;
      const applicable = template.verified && candidate.scope === scope;
      const prior = activeApproval(template.faqId);
      const authorized = record.status === 'review' && applicable && discrepancy
        && (!prior || prior.mailId === template.supersedes);
      return {
        id: template.id, faqId: template.faqId, actor: template.actor, verified: template.verified, date,
        question: template.question, subject: template.subject, body: template.body, scope: candidate.scope,
        ...record, discrepancy, withdrawn: withdrawals.has(template.id),
        canApprove: Boolean(authorized), canRevise: record.status === 'review' && !record.reviewedBody && Boolean(template.corrected),
        clarification: record.status === 'unread' ? null : template.clarification || null,
        correctedBody: record.status === 'review' ? template.corrected?.body || null : null,
        evidence: checked?.sources || [],
        recipientAnswer: checked && applicable && !withdrawals.has(template.id) ? personalAnswer(template, candidate) : null
      };
    }
    function personalAnswer(template, candidate) {
      const {discrepancy, sources} = evaluation(candidate.facts, candidate.scope, revision);
      return { text: template.clarification && candidate !== template ? template.clarification : candidate.body,
        actor: template.actor, date, scope: candidate.scope, mailId: template.id, subject: template.subject, discrepancy, sources };
    }
    function state() {
      return {
        revision, docId: 'DB', documentTitle, synthetic: revision !== 'v1', changedRefs: [...changedRefs], event: event && { ...event },
        clauses: Object.entries(pages[revision]).map(([ref, clause]) => ({
          ref, text: clause.text, originalText: pages[event?.from || revision][ref]?.text || '', changed: changedRefs.includes(ref)
        })),
        faqs: templates.map(template => {
          const {status, answer, version, reason, facts, approval} = faqs.get(template.id);
          const {refs, discrepancy} = evaluation(facts, scope, revision);
          return { id: template.id, question: template.question, status, answer, version, reason, refs, mailId: approval?.mailId || null,
            approval: approval && { ...approval }, discrepancy: status === 'published' ? discrepancy : null,
            withdrawToken: withdrawKey(template.id, faqs.get(template.id), discrepancy) };
        }),
        mails: inbox.map(mailView), log: log.map(line => ({ ...line }))
      };
    }

    function storeFaq(template, candidate, status) {
      const faq = faqs.get(template.faqId);
      const record = mails.get(template.id);
      Object.assign(faq, { status, answer: candidate.body, facts: candidate.facts, version: revision, reason: record.reason,
        approval: { actor: template.actor, date, scope: candidate.scope, simulated: true, mailId: template.id, subject: template.subject,
          verifiedText: candidate === template ? template.body : template.clarification } });
    }

    function checkMail(template) {
      const record = mails.get(template.id);
      record.checkedVersion = revision;
      const check = assessMail(template, record);
      const candidate = record.reviewedBody ? template.corrected : template;
      const prior = activeApproval(template.faqId);
      const unresolvedExpert = prior && template.supersedes !== prior.mailId;
      record.status = 'review';
      record.reviewToken = token();
      if (!template.verified) record.reason = 'Полномочия автора пересланной копии пока не подтверждены.';
      else if (candidate.scope !== scope) record.reason = candidate.scope
        ? 'Разъяснение относится к другой дирекции. Для вопросов ДБРЦТ его применять нельзя.'
        : 'Нужно уточнить, для какого вида оценки дан ответ.';
      else if (unresolvedExpert) record.reason = 'У записи есть другое экспертное основание. Нужно уточнить, какое разъяснение действует.';
      else if (check.discrepancy) record.reason = check.discrepancy.note + ' Эксперт ответил адресату. Перед публикацией общей рекомендации подтвердите область её применения.';
      else if (check.kind === 'covered') {
        record.status = 'covered';
        record.reviewToken = null;
        record.reason = 'Применимый регламент полностью отвечает на вопрос. Обращение сохранено с источником.';
        // Keep the accepted correspondence for the next version comparison.
        storeFaq(template, candidate, 'archived');
      } else {
        record.reviewToken = null;
        record.status = 'published';
        record.reason = 'Проверенное письмо дополняет применимые пункты. Разъяснение опубликовано в FAQ.';
        storeFaq(template, candidate, 'published');
      }
      note('Письмо «' + template.subject + '». ' + record.reason);
    }

    function syncConfluence() {
      const next = events.find(entry => entry.from === revision);
      if (!next) return state();
      const old = pages[revision];
      revision = next.to;
      event = next;
      const page = pages[revision];
      changedRefs = [...new Set([...Object.keys(old), ...Object.keys(page)])].filter(ref => old[ref]?.text !== page[ref]?.text);
      note('Прочитана история страницы ' + pageId + ' в Confluence. ' + next.from + ' → ' + next.to + '.');
      for (const template of templates) {
        const record = faqs.get(template.id);
        if (!record.approval || record.status === 'withdrawn') continue;
        const before = compare(record.facts, scope, old);
        const after = evaluation(record.facts, scope, revision);
        if (![...before.refs, ...after.refs].some(ref => changedRefs.includes(ref))) continue;
        if (after.kind === 'covered') {
          record.status = 'archived';
          record.reason = 'Теперь регламент полностью совпадает с разъяснением. Дубликат автоматически перенесён в архив FAQ.';
        } else {
          record.status = 'published';
          record.reason = after.discrepancy?.note || 'Разъяснение дополняет действующие пункты.';
        }
        note(template.question + ' ' + record.reason);
      }
      for (const template of inbox) {
        if (mails.get(template.id).status === 'review') checkMail(template);
      }
      return state();
    }
    function receiveMail(id) {
      const template = inbox.find(message => message.id === id);
      if (template && mails.get(id).status === 'unread') checkMail(template);
      return state();
    }
    function decideMail(id, expectedToken, action) {
      const template = inbox.find(message => message.id === id);
      const record = mails.get(id);
      if (!record || record.status !== 'review' || !expectedToken || record.reviewToken !== expectedToken || record.checkedVersion !== revision) return state();
      if (action === 'reject') {
        record.status = 'rejected';
        record.reason = 'Передача в общий FAQ отклонена. Ответ в исходном обращении сохранён.';
      } else if (action === 'approve' && mailView(template).canApprove) {
        record.reason = 'Служба качества подтвердила область применения. Исходный ответ эксперта опубликован вместе с отметкой о расхождении.';
        record.status = 'published';
        storeFaq(template, record.reviewedBody ? template.corrected : template, 'published');
      } else if (action === 'revise' && mailView(template).canRevise && template.verified && template.corrected?.scope === scope) {
        record.reviewedBody = template.corrected.body;
        checkMail(template);
        return state();
      } else return state();
      record.reviewToken = null;
      note('Письмо «' + template.subject + '». ' + record.reason);
      return state();
    }
    function withdrawFaq(id, expectedToken) {
      const record = faqs.get(id);
      if (!record || record.status !== 'published' || !expectedToken) return state();
      const {discrepancy} = evaluation(record.facts, scope, revision);
      if (withdrawKey(id, record, discrepancy) !== expectedToken) return state();
      record.status = 'withdrawn';
      withdrawals.add(record.approval.mailId);
      record.reason = 'Служба качества явно отозвала разъяснение. Письмо и решение сохранены в истории.';
      note('Отзыв письма «' + record.approval.subject + '» (' + record.approval.mailId + '). ' + record.reason);
      return state();
    }
    function query(id) {
      const template = templates.find(entry => entry.id === id);
      if (!template) return null;
      const record = faqs.get(id);
      const {refs, sources, discrepancy, addressesAll} = evaluation(record.facts, scope, revision);
      const published = record.status === 'published';
      const byRegulation = ['archived', 'withdrawn'].includes(record.status) && addressesAll;
      const answerKind = published ? 'faq' : byRegulation ? 'regulation' : 'none';
      let answer = 'По этому вопросу пока нет подтверждённого ответа.';
      if (published) answer = record.answer;
      else if (byRegulation) answer = sources.map(source => source.text.slice(source.ref.length + 2)).join('\n\n');
      return {
        id, question: template.question, status: record.status, answerKind, answer, refs, sources: published ? sources : byRegulation ? sources.map(source => ({ ...source, role: 'support' })) : [], documentTitle,
        version: published ? record.version : byRegulation ? revision : null, currentRevision: revision, synthetic: revision !== 'v1',
        approval: published ? { ...record.approval } : null, discrepancy: published ? discrepancy : null
      };
    }
    reset();
    return { state, query, syncConfluence, receiveMail, decideMail, withdrawFaq, reset };
  }
  return { createModel };
});
