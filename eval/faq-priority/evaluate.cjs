'use strict';

// Offline acceptance cases for the demo, independent of the regulation-only model benchmarks.
// Expected actions and amounts below are handwritten, never extracted from the sources under test.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const { createModel } = require('../../demo/faq-revisions/model.js');

const browser = { window: {} };
vm.runInNewContext(readFileSync(join(__dirname, '../../demo/faq-revisions/chat-view.js'), 'utf8'), browser);
const { snapshot } = browser.window.FaqRevisionChat;

function sharedAnswer(model, id = 'estimate') {
  const view = snapshot(model.query(id));
  const primary = view.sources[0];
  return {
    text: view.text, expert: primary?.kind === 'faq' ? primary.mailId : null,
    primaryRef: primary?.kind === 'regulation' ? primary.ref : null,
    citationText: primary?.text, identified: Boolean(primary?.actor && primary?.date),
    record: view.discrepancy, sources: view.sources, note: view.note
  };
}

function currentMail(model, id) {
  return model.state().mails.find(item => item.id === id);
}

function recipientAnswer(model, id) {
  const answer = currentMail(model, id).recipientAnswer;
  if (!answer) return null;
  return {
    text: answer.text, expert: answer.mailId, primaryRef: null,
    citationText: answer.text, identified: Boolean(answer.actor && answer.date && answer.scope),
    record: answer.discrepancy, sources: answer.sources, note: answer.discrepancy?.note
  };
}

function review(model, id, decision) {
  model.decideMail(id, currentMail(model, id).reviewToken, decision);
}

// Exact employee wording works for these fixed demo replies. An open-text LLM
// answer requires a separate semantic grade, not this fixture oracle.
function normalize(text) {
  return text.normalize('NFC').trim().replace(/\s+/g, ' ');
}

function assess(actual, expected) {
  // A regulatory answer can be supported by several clauses.
  const cited = expected.clauses
    ? expected.clauses.every(([ref, text]) => actual?.sources.some(source =>
        source.kind === 'regulation' && source.ref === ref && source.version === expected.revision
        && normalize(source.text) === normalize(ref + '. ' + text)))
    : Boolean(actual?.citationText && normalize(actual.citationText).includes(normalize(expected.text || '')));
  const sourceCorrect = expected.withhold
    ? !actual
    : actual != null && (actual.expert || null) === (expected.expert || null)
      && (actual.primaryRef || null) === (expected.primaryRef || null)
      && (!expected.expert || actual.identified)
      && normalize(actual.text) === normalize(expected.text)
      && cited;
  const conflict = actual?.record;
  const mismatchSources = (actual?.sources || []).filter(item => item.role === 'discrepancy');
  let detectionCorrect = !conflict && !mismatchSources.length;
  if (expected.wiki) {
    const [revision, days] = expected.wiki;
    detectionCorrect = Boolean(conflict && conflict.refs.length === 1 && conflict.refs[0] === '1.6'
      && conflict.revision === revision && actual.note?.trim()
      && mismatchSources.length === 1 && mismatchSources[0].ref === '1.6'
      && (mismatchSources[0].version || mismatchSources[0].revision) === revision
      && days.test(mismatchSources[0].text));
  }
  return { sourceCorrect, detectionCorrect };
}

const checks = [];
function check(name, actual, expected) {
  checks.push({ name, ...assess(actual, expected), expectedConflict: Boolean(expected.wiki) });
}

// Independent gold values for the annotated DB presales example and its versions.
const five = /для точных оценок\s*[–—-]\s*5 рабочих дней/i;
const seven = /для точных оценок\s*[–—-]\s*7 рабочих дней/i;
const oldFaq = {
  expert: 'estimate-initial',
  text: 'Рекомендуемые сроки выдачи оценки в ДБРЦТ — 3 рабочих дня для бюджетной и 5 рабочих дней для точной. При подключении подрядчика дополнительно учитывай SLA на его поиск и согласование.'
};
const newFaq = {
  expert: 'deadline',
  text: 'Уточняем нашу прежнюю рекомендацию для ДБРЦТ. На точную оценку рекомендуем 2 рабочих дня, на бюджетную — 3 рабочих дня. При привлечении подрядчика дополнительно учитывайте SLA на его поиск и согласование.'
};

const mailFirst = createModel();
check('Исходные письмо и регламент согласуются', sharedAnswer(mailFirst), oldFaq);
mailFirst.receiveMail('deadline');
check('До модерации автор исходного обращения получает разъяснение',
  recipientAnswer(mailFirst, 'deadline'), { ...newFaq, wiki: ['v1', five] });
check('До модерации другим доступен прежний FAQ', sharedAnswer(mailFirst), oldFaq);
review(mailFirst, 'deadline', 'approve');
check('После модерации исходное письмо определяет ответ другим сотрудникам',
  sharedAnswer(mailFirst), { ...newFaq, wiki: ['v1', five] });
mailFirst.syncConfluence();
check('Новейшая редакция вики не отменяет принятое разъяснение',
  sharedAnswer(mailFirst), { ...newFaq, wiki: ['v2', seven] });

const wikiFirst = createModel();
wikiFirst.syncConfluence();
check('При чтении новой редакции опубликованный экспертный FAQ остаётся применим',
  sharedAnswer(wikiFirst), { ...oldFaq, wiki: ['v2', seven] });
check('При полном совпадении достаточно нового пункта',
  sharedAnswer(wikiFirst, 'channel'), { primaryRef: 'Д.1', text: 'Отметку о передаче оценки заказчику внесите в поле «Передача оценки» в карточке пресейла.' });
wikiFirst.receiveMail('deadline');
review(wikiFirst, 'deadline', 'approve');
check('Обратный порядок событий даёт тот же результат',
  sharedAnswer(wikiFirst), { ...newFaq, wiki: ['v2', seven] });
wikiFirst.receiveMail('receipt');
check('Разъяснение заполняет пробел в регламенте',
  sharedAnswer(wikiFirst, 'receipt'), { expert: 'receipt', text: 'В теме письма укажите «Подтверждение получения оценки» и номер заявки на пресейл.' });

const controls = createModel();
for (const id of ['unverified', 'other-scope', 'ambiguous']) {
  controls.receiveMail(id);
  check('Письмо ' + id + ' не становится основанием в исходном обращении',
    recipientAnswer(controls, id), { withhold: true });
}
check('Письма с неподтверждёнными полномочиями или областью не вытесняют действующий FAQ',
  sharedAnswer(controls), oldFaq);

// Adversarial controls retain the right citations, amounts and conflict markers,
// but change the employee's actual instruction or the declared controlling source.
const gold = { ...newFaq, wiki: ['v2', seven] };
const valid = sharedAnswer(wikiFirst);
const wrongConclusions = [
  { ...valid, text: valid.text + ' Однако в работе ориентируйтесь на 7 рабочих дней из регламента.' },
  { ...valid, expert: null, primaryRef: '1.6' }
];
for (const answer of wrongConclusions) {
  assert.deepEqual(assess(answer, gold), { sourceCorrect: false, detectionCorrect: true });
}
assert.deepEqual(assess({ ...valid, record: null }, gold), { sourceCorrect: true, detectionCorrect: false });

// Only a distinct decision by Quality withdraws an otherwise applicable answer.
const live = wikiFirst.state().faqs.find(faq => faq.id === 'estimate');
wikiFirst.withdrawFaq('estimate', live.withdrawToken);
const timeline = wikiFirst.state();
const archived = timeline.faqs.find(faq => faq.id === 'estimate');
assert.equal(archived.status, 'withdrawn');
assert.equal(archived.approval.mailId, 'deadline');
assert.equal(normalize(archived.answer), normalize(newFaq.text));
const originalMail = timeline.mails.find(mail => mail.id === 'deadline');
assert.equal(originalMail.withdrawn, true);
assert.equal(normalize(originalMail.body), normalize(newFaq.text));
assert.equal(originalMail.discrepancy.revision, 'v2');
assert.deepEqual(originalMail.discrepancy.refs, ['1.6']);
const clauses = [
  ['1.6', 'Рекомендуемые сроки выдачи: для точных оценок – 7 рабочих дней; для бюджетных оценок – 3 рабочих дня.'],
  ['1.7', 'В случае подключения к оценке Подрядчика необходимо дополнительно учитывать SLA на поиск и согласование подрядчика.']
];
check('После явного отзыва используется действующий регламент, расхождение осталось в истории',
  sharedAnswer(wikiFirst), { primaryRef: '1.6', revision: 'v2', clauses, text: clauses.map(([, text]) => text).join(' ') });

let answerPass = 0, detected = 0, falseAlarms = 0, misses = 0;
for (const row of checks) {
  answerPass += Number(row.sourceCorrect);
  detected += Number(row.expectedConflict && row.detectionCorrect);
  falseAlarms += Number(!row.expectedConflict && !row.detectionCorrect);
  misses += Number(row.expectedConflict && !row.detectionCorrect);
  console.log(`${row.sourceCorrect ? 'OK' : 'FAIL'} ответ | ${row.detectionCorrect ? 'OK' : 'FAIL'} расхождение | ${row.name}`);
}
console.log(`\nВерное основание и содержание: ${answerPass}/${checks.length}. Расхождения с верным пунктом и редакцией: ${detected}/${detected + misses}. Ложных сигналов: ${falseAlarms}. Пропусков или неверных ссылок: ${misses}.`);
if (answerPass !== checks.length || falseAlarms || misses) process.exitCode = 1;
