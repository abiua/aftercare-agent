import assert from 'node:assert/strict';
import test from 'node:test';
import { describeRefundExecution, matchingRefundExecution, readRefundLookup, refundExecutionId } from '../src/refundLookup.ts';

const id = '1b17965e-8ecd-44f2-a983-482e334af445';
const execution = {id, amount_cents: 39800, created_at: '2026-11-10T08:00:00+00:00', status: 'FAILED', receipt_confirmed: true};

test('requires a single canonical-format execution UUID and normalizes case and surrounding whitespace', () => {
  assert.equal(refundExecutionId(`  ${id.toUpperCase()}  `), id);
  for (const value of ['', 'O001', id.slice(1), id.replaceAll('-', ''), `{${id}}`, `${id} ${id}`, `退款执行编号：${id}`]) {
    assert.equal(refundExecutionId(value), undefined);
  }
});

test('only displays a matching single execution and distinguishes local receipt states', () => {
  assert.deepEqual(matchingRefundExecution({schema_version: 'ac-v1', status: 'FOUND', execution}, id), execution);
  assert.equal(matchingRefundExecution({schema_version: 'ac-v1', status: 'FOUND', execution}, 'other'), undefined);
  assert.deepEqual(describeRefundExecution(execution), {status: '模拟退款处理失败', receipt: '本地模拟回执已确认'});
  for (const status of ['UNKNOWN', 'SUBMITTING', 'OTHER']) {
    assert.equal(describeRefundExecution({...execution, status}).receipt, '未确认模拟回执');
  }
  assert.deepEqual(readRefundLookup({status: 'REFERENCE_REQUIRED', execution: null}), {status: 'REFERENCE_REQUIRED', execution: null});
  assert.equal(readRefundLookup({status: 'FOUND', execution: null}), undefined);
});
