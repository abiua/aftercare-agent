import assert from 'node:assert/strict';
import { test } from 'node:test';
import { canActBeforeExecution, orderRefundPresentation } from '../src/casePresentation.ts';

const order = (refunded_cents) => ({items: [{refunded_cents}]});

test('allows approval and simulation only before an execution record or execution state exists', () => {
  const current = {id: 'case', status: 'APPROVED', intent: 'damaged'};
  const mayStart = (ledger, changes = {}) => canActBeforeExecution({case: {...current, ...changes}, ledger}, 'proposal');
  assert.equal(mayStart([]), true);
  for (const status of ['UNKNOWN', 'SUBMITTING', 'FAILED', 'SUCCEEDED']) {
    assert.equal(mayStart([{proposal_id: 'proposal', status}]), false);
  }
  assert.equal(mayStart([{case_id: 'case', proposal_id: 'previous-proposal'}]), false);
  assert.equal(mayStart([{id: 'legacy-row'}]), false);
  for (const status of ['EXECUTION_PENDING', 'EXECUTION_FAILED', 'RESOLVED']) {
    assert.equal(mayStart([], {status}), false);
  }
  assert.equal(mayStart([{action: 'REFUND'}], {intent: 'refund_status'}), true);
});

test('keeps historical totals separate from a checked empty order ledger or a failed read', () => {
  const zero = orderRefundPresentation({ledger: [], order: order(0)}, false);
  const historical = orderRefundPresentation({ledger: [], order: order(35900)}, false);
  assert.deepEqual(zero.notice, {
    type: 'info',
    title: '未查到该订单的退款执行记录',
    description: '是否曾申请、是否处理失败或是否实际到账，需另行核对。',
  });
  assert.equal(zero.historicalRefundedCents, 0);
  assert.deepEqual(historical, {...zero, historicalRefundedCents: 35900});
  const failed = orderRefundPresentation({
    ledger: [{action: 'REFUND', status: 'UNKNOWN', receipt: null}], order: order(35900),
  }, true);
  assert.match(failed.notice.title, /无法更新/);
  assert.deepEqual(failed.rows, []);
  assert.equal(failed.historicalRefundedCents, undefined);
});

test('distinguishes unknown, submitting, and missing receipts from confirmed simulated records', () => {
  const succeeded = {action: 'REFUND', status: 'SUCCEEDED', receipt: {id: 'sim-success'}};
  const failed = {action: 'REFUND', status: 'FAILED', receipt: {id: 'sim-failure'}};
  const ledger = [
    succeeded, failed, {action: 'REFUND', status: 'UNKNOWN', receipt: null},
    {action: 'REFUND', status: 'SUBMITTING', receipt: null},
    {action: 'REFUND', status: 'SUCCEEDED', receipt: {id: ' '}},
    {action: 'REPLACE', status: 'SUCCEEDED', receipt: {id: 'sim-replacement'}},
  ];
  const pending = orderRefundPresentation({ledger, order: order(0)}, false).notice;
  assert.match(pending.description, /5 笔退款执行记录，其中1 笔结果未知、1 笔正在提交、1 笔缺少可确认的模拟回执/);
  assert.equal(pending.type, 'warning');
  const confirmed = orderRefundPresentation({ledger: [succeeded, failed], order: order(0)}, false).notice;
  assert.equal(confirmed.type, 'info');
  assert.match(confirmed.description, /模拟执行结果和回执.*实际到账需另行核对/);
});
