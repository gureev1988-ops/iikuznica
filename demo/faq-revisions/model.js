(function (root, build) {
  const api = build();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.FaqRevisionDemoModel = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  const docId = 'DB';
  const documentTitle = 'Проектная деятельность ДБРЦТ';
  const original = {
    '1.1.1': '1.1.1. При отсутствии в Заявке на пресейл квалификационной анкеты Заявка будет в ожидании информации со стороны Архитектора до её предоставления и не будет взята в работу.',
    '1.6': '1.6. Рекомендуемые сроки выдачи:\nдля точных оценок – 5 рабочих дней;\nдля бюджетных оценок – 3 рабочих дня.',
    '1.7': '1.7. В случае подключения к оценке Подрядчика необходимо дополнительно учитывать SLA на поиск и согласование подрядчика.'
  };
  const revisions = {
    v1: { synthetic: false, clauses: original },
    v2: {
      synthetic: true,
      clauses: { ...original, '1.6': original['1.6'].replace('5 рабочих дней', '7 рабочих дней') }
    }
  };
  const originalEstimate = 'Рекомендуемые сроки выдачи оценки в ДБРЦТ — 3 рабочих дня для бюджетной и 5 рабочих дней для точной. При подключении подрядчика дополнительно учитывай SLA на его поиск и согласование.';
  const revisedEstimate = 'Рекомендуемые сроки выдачи оценки в ДБРЦТ — 3 рабочих дня для бюджетной и 7 рабочих дней для точной. При подключении подрядчика дополнительно учитывай SLA на его поиск и согласование.';
  const definitions = [
    {
      id: 'estimate',
      question: 'Какие сроки рекомендует ДБРЦТ для бюджетной и точной оценки и что учесть при подключении подрядчика?',
      sources: [{ docId, ref: '1.6', role: 'primary' }, { docId, ref: '1.7', role: 'condition' }],
      originalAnswer: originalEstimate,
      reviewedDraft: revisedEstimate
    },
    {
      id: 'questionnaire',
      question: 'Возьмёт ли ДБРЦТ заявку на пресейл в работу без квалификационной анкеты?',
      sources: [{ docId, ref: '1.1.1', role: 'primary' }],
      originalAnswer: 'Без квалификационной анкеты заявку на пресейл в работу не возьмут. Она останется в ожидании информации до предоставления анкеты.'
    }
  ];
  const approval = { actor: 'Уполномоченный сотрудник отдела качества', date: '2026-11-26', simulated: true };
  const definitionsById = new Map(definitions.map(entry => [entry.id, entry]));
  const bySource = new Map();
  function sourceKey(sourceDocId, ref) {
    return sourceDocId + '#' + ref;
  }
  for (const entry of definitions) {
    for (const { docId: sourceDocId, ref } of entry.sources) {
      const key = sourceKey(sourceDocId, ref);
      if (!bySource.has(key)) bySource.set(key, new Set());
      bySource.get(key).add(entry.id);
    }
  }
  let nextInstance = 0;

  function createModel() {
    const instanceId = ++nextInstance;
    let nextReview = 0;
    let revision;
    let changedRefs;
    let records;

    function refsOf(entry) {
      return entry.sources.map(source => source.ref);
    }
    function citations(entry) {
      return entry.sources.map(source => ({
        ...source, documentTitle, revision,
        synthetic: revisions[revision].synthetic,
        text: revisions[revision].clauses[source.ref],
        changed: changedRefs.includes(source.ref)
      }));
    }
    function reset() {
      revision = 'v1';
      changedRefs = [];
      records = new Map(definitions.map(entry => [entry.id, {
        status: 'published',
        answer: entry.originalAnswer,
        version: 'v1',
        approval: { ...approval },
        review: null
      }]));
      return state();
    }
    function state() {
      return {
        docId, documentTitle, revision,
        synthetic: revisions[revision].synthetic,
        changedRefs: [...changedRefs],
        clauses: Object.entries(revisions[revision].clauses).map(([ref, text]) => ({
          ref, text, originalText: original[ref], changed: changedRefs.includes(ref)
        })),
        faqs: definitions.map(entry => {
          const record = records.get(entry.id);
          const review = record.review;
          return {
            id: entry.id, question: entry.question, refs: refsOf(entry),
            status: record.status, version: record.version,
            answer: review ? null : record.answer,
            originalAnswer: entry.originalAnswer,
            draft: review ? review.draft : null,
            reviewToken: review ? review.token : null,
            approval: review ? null : { ...record.approval }
          };
        })
      };
    }
    function importRevision() {
      const targetRevision = 'v2';
      if (revision === targetRevision) return state();
      const before = revisions[revision].clauses;
      const after = revisions[targetRevision].clauses;
      changedRefs = [...new Set([...Object.keys(before), ...Object.keys(after)])]
        .filter(ref => before[ref] !== after[ref]);
      const affected = new Set();
      for (const ref of changedRefs) {
        for (const id of bySource.get(sourceKey(docId, ref)) || []) affected.add(id);
      }
      revision = targetRevision;
      for (const id of affected) {
        const entry = definitionsById.get(id);
        const record = records.get(id);
        record.status = 'review';
        record.review = {
          token: instanceId + '-' + ++nextReview,
          version: targetRevision,
          draft: entry.reviewedDraft,
          sources: citations(entry)
        };
      }
      return state();
    }
    function publish(id, reviewToken) {
      const record = records.get(id);
      const review = record && record.review;
      if (!review || !reviewToken || review.token !== reviewToken || review.version !== revision) return state();
      if (typeof review.draft !== 'string' || !review.draft.trim()) return state();
      if (!review.sources.every(source => revisions[revision].clauses[source.ref] === source.text)) return state();
      record.answer = review.draft;
      record.version = revision;
      record.approval = { ...approval };
      record.status = 'published';
      record.review = null;
      return state();
    }
    function query(id) {
      const entry = definitionsById.get(id);
      if (!entry) return null;
      const record = records.get(id);
      const review = record.review;
      const sources = citations(entry);
      return {
        id, question: entry.question, status: record.status,
        answer: review ? null : record.answer,
        refs: refsOf(entry), version: review ? null : record.version,
        documentTitle, currentRevision: revision, synthetic: revisions[revision].synthetic,
        sources: review ? [] : sources,
        approval: review ? null : { ...record.approval },
        regulationFallback: review ? {
          answer: sources.map(source => source.text.slice(source.ref.length + 2)).join('\n\n'),
          sources,
          previousAnswer: record.answer, previousVersion: record.version,
          previousApproval: { ...record.approval }
        } : null
      };
    }

    reset();
    return { state, query, importRevision, publish, reset };
  }
  return { createModel };
});
