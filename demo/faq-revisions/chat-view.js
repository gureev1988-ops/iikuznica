(function () {
  'use strict';

  const titles = {
    estimate: 'Сроки оценки ДБРЦТ',
    questionnaire: 'Анкета на пресейл',
    channel: 'Где зафиксировать передачу оценки',
    'budget-tag': 'Письмо о бюджетной оценке',
    receipt: 'Подтверждение получения'
  };

  // Capture only the current employee-visible answer and its supporting evidence.
  function snapshot(query) {
    const byFaq = query.answerKind === 'faq';
    const byRule = query.answerKind === 'regulation';
    const demoRevision = query.currentRevision === 'v2';
    const context = demoRevision ? 'Снимок Confluence · демоверсия 2' : 'Снимок Confluence · исходная версия';
    const sources = query.sources.map(source => ({
      label: 'п. ' + source.ref,
      title: source.documentTitle,
      meta: source.revision === 'v2' ? 'Снимок Confluence · демоверсия 2' : 'Снимок Confluence · исходная версия',
      text: source.text,
      ref: source.ref,
      version: source.revision,
      kind: 'regulation'
    }));
    if (byFaq) {
      sources.unshift({
        label: 'Ответ отдела качества', title: query.question,
        meta: ['Утверждённый FAQ', query.approval?.subject && ('Письмо «' + query.approval.subject + '»'),
          query.approval?.mailId && ('Демо-ID ' + query.approval.mailId), query.approval?.actor, query.approval?.date, 'демоданные'].filter(Boolean).join(' · '),
        text: query.approval?.verifiedText || query.answer, version: query.version, kind: 'faq'
      });
    }
    const status = byFaq ? 'Ответ отдела качества' : byRule ? 'Ответ по регламенту' : 'Требуется разъяснение';
    const traceBasis = byFaq ? 'Утверждённый ответ FAQ в области вопроса.' :
      byRule ? 'Действующие пункты опубликованной в Confluence версии.' : 'Подтверждённое основание для ответа отсутствует.';
    const pending = query.status === 'review' || (!byRule && !byFaq);
    return {
      question: query.question, text: query.answer, sources, status, context, traceBasis, pending, demoRevision,
      note: byRule && query.status === 'review' ? 'Служба качества сверяет прежнее разъяснение с действующим регламентом.' : '',
      label: titles[query.id] || query.question
    };
  }
  window.FaqRevisionChat = {snapshot};
}());
