(function () {
  'use strict';

  const titles = {
    estimate: 'Сроки оценки ДБРЦТ',
    questionnaire: 'Анкета на пресейл',
    channel: 'Где зафиксировать передачу оценки',
    'budget-tag': 'Письмо о бюджетной оценке',
    receipt: 'Подтверждение получения'
  };

  // Keep the evidence for this reply independent of subsequent FAQ and wiki changes.
  function snapshot(query) {
    const byExpert = query.answerKind === 'faq';
    const byRule = query.answerKind === 'regulation';
    const discrepancy = query.discrepancy ? {
      ...query.discrepancy,
      refs: [...query.discrepancy.refs],
      differences: query.discrepancy.differences.map(item => ({...item}))
    } : null;
    const sources = query.sources.map(source => ({
      label: 'п. ' + source.ref + (source.role === 'discrepancy' ? ' · расхождение' : ''),
      title: source.documentTitle,
      meta: ['Снимок Confluence', source.revision, source.date].filter(Boolean).join(' · '),
      text: source.text, ref: source.ref, version: source.revision, date: source.date,
      role: source.role || 'support', kind: 'regulation'
    }));
    if (byExpert) {
      const approval = query.approval;
      sources.unshift({
        label: 'Разъяснение эксперта', title: approval.subject || query.question,
        meta: ['Подтверждённый FAQ', approval.actor, approval.date, approval.scope,
          approval.mailId && ('Демо-ID ' + approval.mailId)].filter(Boolean).join(' · '),
        actor: approval.actor, date: approval.date, mailId: approval.mailId,
        text: approval.verifiedText || query.answer, version: query.version, role: 'primary', kind: 'faq'
      });
    }
    const status = byExpert ? 'Ответ службы качества' : byRule ? 'Ответ по регламенту' : 'Требуется разъяснение';
    const traceBasis = discrepancy
      ? 'Подтверждённое разъяснение эксперта применимо к вопросу. Расхождение с вики зафиксировано.'
      : byExpert ? 'Подтверждённое разъяснение эксперта относится к условиям вопроса.'
      : byRule ? 'Ответ содержится в опубликованной редакции Confluence.' : 'Для этой ситуации требуется уточнение.';
    return {
      question: query.question, text: query.answer, sources, status, traceBasis, discrepancy,
      context: 'Демоданные · снимок Confluence ' + query.currentRevision,
      pending: !byExpert && !byRule, demoRevision: query.currentRevision === 'v2',
      note: discrepancy ? discrepancy.note : '', label: titles[query.id] || query.question
    };
  }
  window.FaqRevisionChat = {snapshot};
}());
