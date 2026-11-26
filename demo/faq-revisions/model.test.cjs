'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const { createModel } = require('./model.js');

const estimate = 'estimate';
const questionnaire = 'questionnaire';
const faq = (state, id) => state.faqs.find(entry => entry.id === id);

test('baseline answers link the exact applicable clauses, including contractor SLA', () => {
  const model = createModel();
  const answer = model.query(estimate);
  assert.equal(answer.status, 'published');
  assert.equal(answer.version, 'v1');
  assert.match(answer.answer, /3 рабочих дня/);
  assert.match(answer.answer, /5 рабочих дней/);
  assert.match(answer.answer, /поиск и согласование/);
  assert.deepEqual(answer.sources.map(source => [source.docId, source.ref, source.role]), [
    ['DB', '1.6', 'primary'], ['DB', '1.7', 'condition']
  ]);
  assert.ok(answer.sources.every(source => source.synthetic === false && source.revision === 'v1'));
  assert.equal(answer.approval.simulated, true);
  assert.equal(answer.regulationFallback, null);
  assert.deepEqual(model.query(questionnaire).refs, ['1.1.1']);
  assert.equal(model.query('unknown'), null);
});

test('demo import changes only one digit in one clause and suspends only the dependent FAQ', () => {
  const model = createModel();
  const old = model.query(estimate);
  const independent = model.query(questionnaire);
  const updated = model.importRevision();
  assert.equal(updated.synthetic, true);
  assert.deepEqual(updated.changedRefs, ['1.6']);
  for (const clause of updated.clauses) {
    assert.equal(clause.text, clause.ref === '1.6' ? clause.originalText.replace('5', '7') : clause.originalText);
    assert.equal(clause.changed, clause.ref === '1.6');
  }
  assert.equal(faq(updated, estimate).status, 'review');
  assert.equal(faq(updated, estimate).answer, null);
  assert.equal(faq(updated, questionnaire).status, 'published');
  const answer = model.query(estimate);
  assert.deepEqual([answer.answer, answer.approval, answer.version, answer.sources], [null, null, null, []]);
  const fallback = answer.regulationFallback;
  assert.equal(fallback.previousAnswer, old.answer);
  assert.equal(fallback.previousVersion, 'v1');
  assert.deepEqual(fallback.previousApproval, old.approval);
  assert.match(fallback.answer, /7 рабочих дней/);
  assert.match(fallback.answer, /3 рабочих дня/);
  assert.match(fallback.answer, /SLA на поиск и согласование/);
  assert.ok(fallback.sources.every(source => source.synthetic && source.revision === 'v2'));
  assert.deepEqual(fallback.sources.map(source => source.changed), [true, false]);
  const unaffected = model.query(questionnaire);
  assert.equal(unaffected.answer, independent.answer);
  assert.equal(unaffected.version, 'v1');
  assert.deepEqual(unaffected.approval, independent.approval);
  assert.equal(unaffected.sources[0].text, independent.sources[0].text);
  assert.equal(unaffected.sources[0].revision, 'v2');
  assert.equal(unaffected.sources[0].changed, false);
});

test('only an explicit confirmation matching the open review can replace the FAQ', () => {
  const model = createModel();
  const baseline = model.state();
  assert.deepEqual(model.publish(estimate, null), baseline);
  const pending = model.importRevision();
  const token = faq(pending, estimate).reviewToken;
  assert.deepEqual(model.importRevision(), pending);
  assert.deepEqual(model.publish(questionnaire, token), pending);
  assert.deepEqual(model.publish(estimate), pending);
  assert.deepEqual(model.publish(estimate, 'obsolete'), pending);
  const updated = model.publish(estimate, token);
  const answer = model.query(estimate);
  assert.equal(answer.status, 'published');
  assert.equal(answer.version, 'v2');
  assert.equal(answer.regulationFallback, null);
  assert.equal(answer.answer, faq(pending, estimate).draft);
  assert.match(answer.answer, /7 рабочих дней/);
  assert.ok(!answer.answer.includes('5 рабочих дней'));
  assert.ok(answer.sources.every(source => source.synthetic && source.revision === 'v2'));
  assert.equal(answer.approval.simulated, true);
  assert.equal(faq(updated, estimate).reviewToken, null);
  assert.deepEqual(model.publish(estimate, token), updated);
  assert.deepEqual(model.importRevision(), updated);
  assert.deepEqual(model.reset(), baseline);
});

test('a reset and re-import invalidate stale clicks, even if the same revision is pending', () => {
  const model = createModel();
  const first = faq(model.importRevision(), estimate).reviewToken;
  model.reset();
  const secondState = model.importRevision();
  const second = faq(secondState, estimate).reviewToken;
  assert.notEqual(first, second);
  assert.deepEqual(model.publish(estimate, first), secondState);
  const other = createModel();
  const otherToken = faq(other.importRevision(), estimate).reviewToken;
  assert.deepEqual(model.publish(estimate, otherToken), secondState);
  assert.equal(faq(model.publish(estimate, second), estimate).status, 'published');
});

test('changes to returned results cannot affect reviewed drafts, citations or future answers', () => {
  const model = createModel();
  const pending = model.importRevision();
  faq(pending, estimate).draft = 'Ошибка';
  pending.clauses[0].text = 'Ошибка';
  pending.changedRefs.push('1.1.1');
  model.query(estimate).regulationFallback.sources[0].text = 'Ошибка';
  model.query(estimate).regulationFallback.previousApproval.actor = 'Ошибка';
  model.publish(estimate, faq(pending, estimate).reviewToken);
  const answer = model.query(estimate);
  assert.match(answer.answer, /7 рабочих дней/);
  assert.match(answer.sources[0].text, /7 рабочих дней/);
  assert.equal(answer.approval.actor, 'Уполномоченный сотрудник отдела качества');
  assert.deepEqual(model.state().changedRefs, ['1.6']);
});

test('script exports a browser global with no Node or network dependency', () => {
  const shell = { window: {} };
  vm.runInNewContext(readFileSync(join(__dirname, 'model.js'), 'utf8'), shell);
  const model = shell.window.FaqRevisionDemoModel.createModel();
  assert.equal(model.query(estimate).version, 'v1');
  assert.equal(model.query(estimate).regulationFallback, null);
});
