(function () {
  'use strict';

  // A query produces an immutable chat snapshot; older messages keep their original evidence.
  function snapshot(query) {
    const inReview = query.status === 'review';
    const fallback = inReview ? query.regulationFallback : null;
    const approval = inReview ? fallback.previousApproval : query.approval;
    const faqText = inReview ? fallback.previousAnswer : query.answer;
    const faqVersion = inReview ? fallback.previousVersion : query.version;
    const clauses = inReview ? fallback.sources : query.sources;
    const refs = clauses.map(source => ({
      label: 'п. ' + source.ref,
      title: source.documentTitle,
      meta: source.revision === 'v2' ? 'Демонстрационная редакция 2' : 'Исходная редакция · корпус от 26.11.2026',
      text: source.text,
      ref: source.ref,
      version: source.revision,
      kind: 'regulation'
    }));
    const faq = {
      label: inReview ? 'Прежний ответ FAQ' : 'Ответ отдела качества',
      title: query.question,
      meta: [inReview ? 'FAQ на пересмотре' : 'Согласованный FAQ', approval.actor, approval.date,
             'демонстрационные данные'].join(' · '),
      text: faqText, version: faqVersion, kind: 'faq'
    };
    return {
      question: query.question,
      text: inReview ? fallback.answer : query.answer,
      status: inReview ? 'FAQ на пересмотре' : 'Ответ отдела качества',
      note: inReview ? 'В прежнем FAQ для точной оценки указано 5 рабочих дней. Действует демонстрационная редакция с новым сроком. Отдел качества проверяет разъяснение.' : '',
      sources: inReview ? [...refs, faq] : [faq, ...refs],
      pending: inReview,
      demoRevision: query.currentRevision === 'v2',
      label: query.id === 'estimate' ? 'Сроки оценки ДБРЦТ' : 'Анкета на пресейл'
    };
  }
  window.FaqRevisionChat = {snapshot};
}());
