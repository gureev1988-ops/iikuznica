'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const { createModel } = require('./model.js');

const faq = (state, id) => state.faqs.find(item => item.id === id);
const mail = (state, id) => state.mails.find(item => item.id === id);

test('external version history drives full coverage and contradiction differently', () => {
  const model = createModel();
  const before = model.state();
  assert.equal(before.event, null);
  assert.equal(model.query('channel').answerKind, 'faq');
  assert.match(model.query('estimate').answer, /5 рабочих дней/);
  const after = model.syncConfluence();
  assert.deepEqual([after.event.pageId, after.event.from, after.event.to], ['DEMO-DB', 'v1', 'v2']);
  assert.deepEqual(after.changedRefs, ['1.6', 'Д.1']);
  assert.equal(after.clauses.find(c => c.ref === 'Д.1').originalText, '');
  assert.equal(faq(after, 'channel').status, 'archived');
  assert.equal(faq(after, 'estimate').status, 'review');
  assert.equal(faq(after, 'questionnaire').status, 'published');
  assert.equal(faq(after, 'receipt').status, 'absent');
  assert.equal(model.query('channel').answerKind, 'regulation');
  assert.deepEqual(model.query('channel').refs, ['Д.1']);
  assert.deepEqual(model.syncConfluence(), after);
});

test('while an FAQ is contested the employee sees current clauses without old FAQ or email', () => {
  const model = createModel();
  model.syncConfluence();
  const result = model.query('estimate');
  assert.equal(result.answerKind, 'regulation');
  assert.match(result.answer, /7 рабочих дней/);
  assert.match(result.answer, /3 рабочих дня/);
  assert.match(result.answer, /SLA на поиск и согласование/);
  assert.doesNotMatch(JSON.stringify(result), /5 рабочих дней/);
  assert.deepEqual(result.sources.map(item => [item.ref, item.revision]), [['1.6', 'v2'], ['1.7', 'v2']]);
  assert.equal(result.approval, null);
  assert.equal(model.query('questionnaire').answerKind, 'faq');
});

test('FAQ removal is explicit, version-bound and idempotent, and never edits Confluence', () => {
  const model = createModel();
  const first = model.syncConfluence();
  const key = faq(first, 'estimate').reviewToken;
  assert.deepEqual(model.decideFaq('estimate', null, 'remove'), first);
  assert.deepEqual(model.decideFaq('estimate', key, 'publish'), first);
  const deferred = model.decideFaq('estimate', key, 'defer');
  assert.equal(faq(deferred, 'estimate').status, 'review');
  assert.deepEqual(model.decideFaq('estimate', key, 'remove'), deferred);
  assert.equal(model.query('estimate').answerKind, 'regulation');
  const updated = model.decideFaq('estimate', faq(deferred, 'estimate').reviewToken, 'remove');
  assert.equal(faq(updated, 'estimate').status, 'archived');
  assert.match(faq(updated, 'estimate').answer, /5 рабочих дней/);
  assert.deepEqual(updated.event, first.event);
  assert.deepEqual(updated.clauses, first.clauses);
  assert.deepEqual(model.decideFaq('estimate', key, 'remove'), updated);
});

test('a conflicting letter cannot be passed through as an FAQ, including after rejection', () => {
  const model = createModel();
  const initialAnswer = model.query('estimate');
  const held = model.receiveMail('deadline');
  const candidate = mail(held, 'deadline');
  assert.equal(candidate.status, 'review');
  assert.equal(candidate.evidence[0].ref, '1.6');
  assert.deepEqual(model.receiveMail('deadline'), held);
  assert.deepEqual(model.decideMail('deadline', candidate.reviewToken, 'publish'), held);
  assert.deepEqual(model.query('estimate'), initialAnswer);
  const rejected = model.decideMail('deadline', candidate.reviewToken, 'reject');
  assert.equal(mail(rejected, 'deadline').status, 'rejected');
  assert.equal(mail(rejected, 'deadline').body, candidate.body);
  assert.deepEqual(model.receiveMail('deadline'), rejected);
  assert.deepEqual(model.query('estimate'), initialAnswer);
});

test('reviewing a correction requires the current source version; a duplicate is closed', () => {
  const model = createModel();
  const old = mail(model.receiveMail('deadline'), 'deadline');
  const current = model.syncConfluence();
  const pending = mail(current, 'deadline');
  assert.notEqual(old.reviewToken, pending.reviewToken);
  assert.equal(pending.checkedVersion, 'v2');
  assert.match(pending.evidence[0].text, /7 рабочих дней/);
  assert.match(pending.correctedBody, /7 рабочих дней/);
  assert.match(pending.body, /2 рабочих дня/);
  assert.deepEqual(model.decideMail('deadline', old.reviewToken, 'revise'), current);
  const accepted = model.decideMail('deadline', pending.reviewToken, 'revise');
  assert.equal(mail(accepted, 'deadline').status, 'covered');
  assert.equal(mail(accepted, 'deadline').reviewedBody, pending.correctedBody);
  assert.equal(faq(accepted, 'estimate').status, 'review');
  assert.equal(model.query('estimate').answerKind, 'regulation');
});

test('an authorized email can fill a gap and survives an unrelated version change', () => {
  const model = createModel();
  assert.equal(model.query('receipt').answerKind, 'none');
  const updated = model.receiveMail('receipt');
  assert.equal(mail(updated, 'receipt').status, 'published');
  assert.equal(faq(updated, 'receipt').status, 'published');
  const verified = model.query('receipt');
  assert.equal(verified.answerKind, 'faq');
  assert.equal(verified.answer, mail(updated, 'receipt').body);
  assert.equal(verified.approval.actor, mail(updated, 'receipt').actor);
  assert.deepEqual(verified.sources, []);
  assert.equal(verified.approval.mailId, 'receipt');
  assert.equal(verified.approval.verifiedText, mail(updated, 'receipt').body);
  assert.deepEqual(model.receiveMail('receipt'), updated);
  model.syncConfluence();
  assert.equal(model.query('receipt').answer, verified.answer);
  assert.equal(mail(model.state(), 'receipt').checkedVersion, 'v1');
});

test('unclear scope triggers moderation and an author-confirmed scoped supplement can publish', () => {
  const model = createModel();
  const held = model.receiveMail('ambiguous');
  const candidate = mail(held, 'ambiguous');
  assert.equal(candidate.status, 'review');
  assert.equal(candidate.canRevise, true);
  assert.ok(candidate.clarification);
  assert.equal(model.query('budget-tag').answerKind, 'none');
  assert.deepEqual(model.decideMail('ambiguous', candidate.reviewToken, 'publish'), held);
  const resolved = model.decideMail('ambiguous', candidate.reviewToken, 'revise');
  assert.equal(mail(resolved, 'ambiguous').status, 'published');
  const answer = model.query('budget-tag');
  assert.match(answer.answer, /бюджетной оценки без подрядчика/);
  assert.match(answer.answer, /3 рабочих дня/);
  assert.match(answer.answer, /В теме письма укажите «Бюджет»/);
  assert.equal(answer.sources[0].ref, '1.6');
  assert.notEqual(answer.answer, candidate.body);
  assert.equal(answer.approval.subject, candidate.subject);
  assert.equal(answer.approval.verifiedText, candidate.clarification);
  assert.equal(mail(resolved, 'ambiguous').body, candidate.body);
  const refreshed = model.syncConfluence();
  assert.equal(faq(refreshed, 'budget-tag').status, 'published');
  assert.equal(model.query('budget-tag').answer, answer.answer);
});

test('old cards and copies of snapshots cannot change live decisions', () => {
  const model = createModel();
  const stale = faq(model.syncConfluence(), 'estimate').reviewToken;
  const baseline = model.reset();
  assert.equal(baseline.event, null);
  const other = createModel();
  const foreign = faq(other.syncConfluence(), 'estimate').reviewToken;
  const pending = model.syncConfluence();
  assert.deepEqual(model.decideFaq('estimate', stale, 'remove'), pending);
  assert.deepEqual(model.decideFaq('estimate', foreign, 'remove'), pending);
  pending.clauses[0].text = 'stale';
  pending.event.actor = '';
  faq(pending, 'estimate').status = 'published';
  model.query('estimate').sources[0].text = '';
  assert.equal(model.query('estimate').status, 'review');
  assert.equal(model.state().event.actor.length > 0, true);
  assert.ok(model.query('estimate').sources[0].text);
  assert.equal(model.query('irrelevant'), null);
});

test('the same model runs offline as a browser global', () => {
  const shell = { window: {} };
  vm.runInNewContext(readFileSync(join(__dirname, 'model.js'), 'utf8'), shell);
  const model = shell.window.FaqRevisionDemoModel.createModel();
  assert.equal(model.query('estimate').answerKind, 'faq');
  assert.equal(model.syncConfluence().revision, 'v2');
});
