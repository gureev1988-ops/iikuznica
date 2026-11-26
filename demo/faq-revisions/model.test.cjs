'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const { createModel } = require('./model.js');

const faq = (state, id) => state.faqs.find(item => item.id === id);
const mail = (state, id) => state.mails.find(item => item.id === id);
const scope = 'ДБРЦТ · пресейл';

test('a new wiki version preserves the approved expert answer, archives an equivalent FAQ, and leaves unrelated records alone', () => {
  const m = createModel();
  const original = m.query('estimate');
  assert.equal(original.discrepancy, null);
  const update = m.syncConfluence();
  const active = m.query('estimate');
  assert.equal(active.answer, original.answer);
  assert.deepEqual(active.approval, original.approval);
  assert.equal(active.approval.verifiedText, active.answer);
  assert.equal(active.approval.mailId, 'estimate-initial');
  assert.equal(active.status, 'published');
  assert.deepEqual(active.discrepancy.differences, [{ref:'1.6',key:'exactDays',expert:5,regulation:7}]);
  assert.deepEqual(active.sources.map(s => [s.ref,s.role,s.revision,s.date]), [
    ['1.6','discrepancy','v2',update.event.date],['1.7','support','v2',update.event.date]
  ]);
  assert.equal(faq(update,'channel').status, 'archived');
  assert.equal(m.query('channel').answerKind, 'regulation');
  assert.deepEqual(m.query('channel').refs, ['Д.1']);
  assert.equal(m.query('questionnaire').discrepancy, null);
  assert.equal(m.query('questionnaire').answerKind, 'faq');
  assert.equal(m.query('receipt').answerKind,'none');
  assert.deepEqual(m.syncConfluence(), update);
});

for (const versionFirst of [true,false]) {
  test('a conflicting reply reaches its addressee before FAQ moderation (' + (versionFirst ? 'wiki first' : 'mail first') + ')', () => {
    const m = createModel();
    if (versionFirst) m.syncConfluence();
    const earlier = m.query('estimate');
    const held = mail(m.receiveMail('deadline'),'deadline');
    assert.equal(held.status, 'review');
    assert.equal(held.canApprove,true);
    assert.equal(held.canRevise,false);
    assert.equal(held.recipientAnswer.text, held.body);
    assert.equal(held.recipientAnswer.actor, held.actor);
    assert.equal(held.recipientAnswer.discrepancy.differences[0].expert, 2);
    assert.equal(held.recipientAnswer.discrepancy.differences[0].regulation, versionFirst ? 7 : 5);
    assert.deepEqual(held.recipientAnswer.sources.map(s => s.ref), ['1.6','1.7']);
    assert.deepEqual(m.query('estimate'), earlier);
    const accepted = m.decideMail('deadline',held.reviewToken,'approve');
    const active = m.query('estimate');
    assert.equal(mail(accepted,'deadline').status, 'published');
    assert.equal(active.answer, held.body);
    assert.equal(active.approval.mailId,'deadline');
    assert.equal(active.approval.verifiedText,held.body);
    assert.equal(active.approval.date,held.date);
    assert.deepEqual(active.discrepancy.refs,['1.6']);
    assert.match(active.answer,/3 рабочих дня/);
    assert.match(active.answer,/поиск и согласование/);
    assert.deepEqual(m.receiveMail('deadline'),accepted);
    assert.deepEqual(m.decideMail('deadline',held.reviewToken,'reject'),accepted);
    m.syncConfluence();
    assert.equal(m.query('estimate').answer,held.body);
    assert.equal(m.query('estimate').discrepancy.differences[0].regulation,7);
    assert.equal(mail(m.state(),'deadline').status, 'published');
  });
}

test('outdated or foreign moderation tokens cannot replace the current FAQ', () => {
  const m = createModel(), other = createModel();
  const old = mail(m.receiveMail('deadline'),'deadline');
  const current = m.syncConfluence();
  const newMail = mail(current,'deadline');
  assert.notEqual(old.reviewToken, newMail.reviewToken);
  assert.deepEqual(m.decideMail('deadline',old.reviewToken,'approve'),current);
  const foreign = mail(other.receiveMail('deadline'),'deadline');
  assert.deepEqual(m.decideMail('deadline',foreign.reviewToken,'approve'),current);
  assert.equal(newMail.checkedVersion, 'v2');
  assert.equal(newMail.recipientAnswer.discrepancy.differences[0].regulation,7);
  const rejected = m.decideMail('deadline',newMail.reviewToken,'reject');
  assert.deepEqual(m.receiveMail('deadline'),rejected);
  assert.equal(mail(rejected,'deadline').status,'rejected');
  assert.equal(mail(rejected,'deadline').recipientAnswer.text,old.body);
  assert.notEqual(m.query('estimate').answer,old.body);
});

test('authority is checked independently of letter wording, and different directorates do not conflict', () => {
  const m = createModel();
  const initial = m.query('estimate');
  for (const id of ['unverified','other-scope']) {
    const state = m.receiveMail(id);
    const candidate = mail(state,id);
    assert.equal(candidate.status,'review');
    assert.equal(candidate.canApprove,false);
    assert.equal(candidate.recipientAnswer,null);
    assert.equal(candidate.discrepancy,null);
    assert.deepEqual(m.decideMail(id,candidate.reviewToken,'approve'),state);
    assert.deepEqual(m.query('estimate'),initial);
    if (id === 'other-scope') {
      assert.equal(candidate.question, initial.question);
      assert.deepEqual(candidate.evidence,[]);
    }
  }
});

test('an ambiguous expert statement stays out until the author identifies the applicable case', () => {
  const m = createModel();
  const hold = mail(m.receiveMail('ambiguous'),'ambiguous');
  assert.equal(hold.recipientAnswer,null);
  assert.equal(hold.canApprove,false);
  assert.equal(hold.canRevise,true);
  assert.equal(m.query('budget-tag').answerKind,'none');
  assert.equal(m.decideMail('ambiguous',hold.reviewToken,'approve').mails.find(item => item.id === 'ambiguous').status,'review');
  m.decideMail('ambiguous',hold.reviewToken,'revise');
  const clarified = m.query('budget-tag');
  assert.match(clarified.answer,/бюджетной оценки без подрядчика/);
  assert.match(clarified.answer,/3 рабочих дня/);
  assert.equal(clarified.approval.verifiedText,hold.clarification);
  assert.equal(clarified.discrepancy,null);
  m.syncConfluence();
  assert.equal(m.query('budget-tag').discrepancy,null);
  assert.match(m.query('budget-tag').answer,/Бюджет/);
});

test('a clarification that disagrees still needs confirmation, including the first FAQ on its subject', () => {
  const candidate = {
    id:'first', faqId:'budget-tag', scope:null, verified:true, actor:'Expert', subject:'Budget', question:'Budget?',
    body:'Two days?', facts:{budgetDays:2}, clarification:'Yes, two days for budget assessments.',
    corrected:{scope,body:'Two days for budget assessments.',facts:{budgetDays:2}}
  };
  const m = createModel({inbox:[candidate]});
  const held = mail(m.receiveMail('first'),'first');
  const reviewed = mail(m.decideMail('first',held.reviewToken,'revise'),'first');
  assert.equal(reviewed.status,'review');
  assert.equal(reviewed.canRevise,false);
  assert.equal(reviewed.canApprove,true);
  assert.match(reviewed.recipientAnswer.text,/Yes, two/);
  assert.equal(reviewed.discrepancy.differences[0].key,'budgetDays');
  assert.equal(m.query('budget-tag').answerKind,'none');
  m.decideMail('first', reviewed.reviewToken,'approve');
  assert.equal(m.query('budget-tag').answer, candidate.corrected.body);
  assert.equal(m.query('budget-tag').approval.verifiedText, candidate.clarification);
  assert.equal(m.query('budget-tag').discrepancy.differences[0].expert,2);
});

test('a separate expert opinion cannot silently replace another expert when applicability is clarified', () => {
  const candidate = {
    id:'second', faqId:'estimate', scope:null, verified:true, actor:'Another expert', subject:'Estimate', question:'Estimate?',
    body:'One day?', facts:{exactDays:1}, clarification:'One day for exact assessments.',
    corrected:{scope,body:'One day for exact assessments.',facts:{exactDays:1}}
  };
  const m = createModel({inbox:[candidate]});
  const original = m.query('estimate');
  const reason = mail(m.receiveMail('second'),'second');
  const reviewed = mail(m.decideMail('second',reason.reviewToken,'revise'),'second');
  assert.equal(reviewed.status,'review');
  assert.equal(reviewed.canApprove,false);
  assert.match(reviewed.reason,/другое экспертное основание/);
  m.decideMail('second',reviewed.reviewToken,'approve');
  assert.deepEqual(m.query('estimate'),original);
});

test('the regulation may catch up and diverge again without losing the retained approved letter', () => {
  const page = days => ({'1.6':{text:'1.6. Сроки '+days+' и 3.',facts:{exactDays:days,budgetDays:3}},
    '1.7':{text:'1.7. Дополнительно учитываем время на поиск и согласование подрядчика.', facts:{contractorSla:'search-and-agree'}}});
  const m = createModel({pages:{v1:page(6),v2:page(5),v3:page(7)}, events:[
    {from:'v1',to:'v2',pageId:'test',date:'2026-11-25'},{from:'v2',to:'v3',pageId:'test',date:'2026-11-26'}
  ]});
  const original = m.query('estimate');
  assert.equal(original.discrepancy.differences[0].regulation,6);
  m.syncConfluence();
  assert.equal(m.query('estimate').status,'archived');
  assert.equal(m.query('estimate').discrepancy,null);
  assert.equal(m.query('estimate').answerKind,'regulation');
  m.syncConfluence();
  assert.equal(m.query('estimate').status,'published');
  assert.deepEqual(m.query('estimate').approval,original.approval);
  assert.equal(m.query('estimate').answer,original.answer);
  assert.equal(m.query('estimate').discrepancy.differences[0].regulation,7);
});

test('a scoped expert can fill a gap, and unrelated changes create no discrepancies', () => {
  const m = createModel();
  const updated = m.receiveMail('receipt');
  const source = mail(updated,'receipt');
  assert.equal(source.status,'published');
  assert.equal(m.query('receipt').approval.mailId,'receipt');
  assert.equal(m.query('receipt').answer, source.body);
  assert.equal(m.query('receipt').discrepancy,null);
  assert.deepEqual(m.query('receipt').sources,[]);
  assert.deepEqual(m.receiveMail('receipt'),updated);
  m.syncConfluence();
  assert.equal(m.query('receipt').answer,source.body);
  assert.equal(m.query('receipt').discrepancy,null);
  assert.equal(mail(m.state(),'receipt').checkedVersion,'v1');
});

test('old snapshots remain independent, and browser and Node have the same policy', () => {
  const shell = {window:{}};
  vm.runInNewContext(readFileSync(join(__dirname,'model.js'),'utf8'),shell);
  const m = shell.window.FaqRevisionDemoModel.createModel();
  const prior = m.query('estimate');
  m.syncConfluence();
  const next = m.query('estimate');
  assert.equal(prior.discrepancy,null);
  assert.equal(next.discrepancy.revision,'v2');
  next.approval.verifiedText = 'changed';
  next.sources[0].text = '';
  next.discrepancy.differences[0].expert = 999;
  assert.equal(m.query('estimate').discrepancy.differences[0].expert,5);
  assert.equal(m.query('estimate').approval.verifiedText,prior.answer);
  assert.ok(m.query('estimate').sources[0].text);
  assert.equal(m.query('irrelevant'),null);
});

test('an explicit quality withdrawal retires one accepted reply and keeps its evidence in history', () => {
  const m = createModel();
  const hold = mail(m.receiveMail('deadline'),'deadline');
  const publication = m.decideMail('deadline',hold.reviewToken,'approve');
  const firstKey = faq(publication,'estimate').withdrawToken;
  const previousAnswer = m.query('estimate');
  const current = m.syncConfluence();
  const key = faq(current,'estimate').withdrawToken;
  assert.ok(key);
  assert.notEqual(firstKey,key);
  assert.deepEqual(m.withdrawFaq('estimate',firstKey),current);
  assert.deepEqual(m.withdrawFaq('channel',key),current);
  const withdrawn = m.withdrawFaq('estimate',key);
  assert.equal(faq(withdrawn,'estimate').status,'withdrawn');
  assert.equal(faq(withdrawn,'estimate').approval.verifiedText,previousAnswer.answer);
  assert.ok(withdrawn.log.at(-1).text.includes('deadline'));
  assert.equal(mail(withdrawn,'deadline').withdrawn,true);
  assert.equal(mail(withdrawn,'deadline').recipientAnswer,null);
  assert.equal(mail(withdrawn,'deadline').body,hold.body);
  const now = m.query('estimate');
  assert.equal(now.answerKind,'regulation');
  assert.equal(now.discrepancy,null);
  assert.equal(now.approval,null);
  assert.match(now.answer,/7 рабочих дней/);
  assert.match(now.answer,/3 рабочих дня/);
  assert.match(now.answer,/поиск и согласование/);
  assert.doesNotMatch(now.answer,/2 рабочих дня/);
  assert.ok(now.sources.every(source=>source.role==='support'));
  assert.deepEqual(m.withdrawFaq('estimate',key),withdrawn);
  assert.equal(previousAnswer.answerKind,'faq');
  assert.deepEqual(m.syncConfluence(),withdrawn);
});

test('withdrawal before another wiki revision is permanent and partial coverage cannot answer the whole question', () => {
  const candidate = {
    id:'partial', faqId:'budget-tag', scope, verified:true, actor:'Expert', subject:'Budget exception', question:'Budget?',
    body:'Two days, mark Budget.', facts:{budgetDays:2,budgetTag:'budget'}
  };
  const m = createModel({inbox:[candidate]});
  const held = mail(m.receiveMail('partial'),'partial');
  const approved = m.decideMail('partial',held.reviewToken,'approve');
  m.withdrawFaq('budget-tag',faq(approved,'budget-tag').withdrawToken);
  assert.equal(m.query('budget-tag').answerKind,'none');
  assert.deepEqual(m.query('budget-tag').sources,[]);
  m.syncConfluence();
  assert.equal(m.query('budget-tag').status,'withdrawn');
  assert.equal(m.query('budget-tag').answerKind,'none');
  assert.equal(mail(m.state(),'partial').withdrawn,true);
});
