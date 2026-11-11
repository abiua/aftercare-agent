import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { readReviewEvidence } from '../src/reviewReading.ts';

const expected = {
  'DEV-HR-01': {steps: ['recorded', 'recorded', 'recorded'], actions: ['已上传商品照片', '已上传包裹照片', '客服审核照片通过']},
  'DEV-HR-02': {steps: ['recorded'], actions: []},
  'DEV-HR-03': {steps: ['recorded'], actions: []},
  'DEV-HR-04': {steps: ['recorded', 'recorded', 'empty'], actions: ['已上传商品照片', '已上传包裹照片', '客服审核照片通过']},
};

const repo = process.env.AFTERCARE_REVIEW_REPORT_ROOT;
test('four saved development reports separate public opinions from internal notes and end-of-report facts', () => {
  const root = repo ? pathToFileURL(resolve(repo) + '/') : new URL('../../', import.meta.url);
  const read = (file) => JSON.parse(readFileSync(new URL(file, root), 'utf8'));
  const {entries} = repo ? read('data/review/development-review.json')
    : JSON.parse(readFileSync(new URL('./fixtures/review-reading.json', import.meta.url), 'utf8'));
  assert.equal(entries.length, 4);
  for (const {review_id, provenance, user_input, result: fixture} of entries) {
    const raw = repo ? read(provenance.report).results.find(({id}) => id === provenance.result_id) : fixture;
    // Object serialization order is deliberately changed. The script trace owns chronology.
    raw.actual.events = Object.fromEntries(Object.entries(raw.actual.events).reverse());
    const result = readReviewEvidence(raw);
    assert.equal(result.customerQuestion, user_input.text);
    assert.deepEqual(result.runs.map(({state}) => state), expected[review_id].steps);
    assert.deepEqual(result.actions.map(({summary}) => summary), expected[review_id].actions);
    assert.equal(result.runs[0].id, 'e02');
    assert.ok(result.internalNotes.length > 0);
    assert.equal(result.closeout, raw.actual.final_business);
    if (review_id === 'DEV-HR-01') {
      assert.match(result.runs.at(-1).output.notes, /请客服确认/);
      assert.ok(result.internalNotes.some(({notes}) => notes.includes('人工审批状态为已批准')));
    }
    if (review_id === 'DEV-HR-02') {
      assert.deepEqual(result.runs[0].output.missing_materials, ['WRONG_ITEM', 'LABEL']);
    }
    if (review_id === 'DEV-HR-03') {
      assert.deepEqual(result.runs[0].output.ledger, []);
      assert.equal(result.runs[0].output.notes, '退款进度仅根据当前模拟账本反馈。');
    }
    if (review_id === 'DEV-HR-04') assert.equal(result.runs.at(-1).output, undefined);
  }
});

test('an unverified question, ambiguous steps and missing output are kept distinct', () => {
  const result = readReviewEvidence({
    trace: [
      {event_id: 'rejected', op: 'create_case', actor: 'customer', error: 'ORDER_DENIED'},
      {event_id: 'ambiguous', op: 'start', actor: 'support'},
      {event_id: 'ambiguous', op: 'resume', actor: 'support'},
      {event_id: 'missing', op: 'resume', actor: 'support'},
    ],
    actual: {events: {
      rejected: {description: '不可用于展示', id: 'A', order_id: 'B'},
      ambiguous: {id: 'run', thread_id: 'thread', case_id: 'case', output: {notes: '顺序不明'}},
      missing: {status: 'NEEDS_REVIEW'},
    }, final_business: {case: {description: '不能代替原话'}}},
  });
  assert.equal(result.customerQuestion, '');
  assert.deepEqual(result.runs.map(({id, state, position}) => [id, state, position]), [
    ['missing', 'missing', 4], ['ambiguous', 'recorded', undefined],
  ]);
});
