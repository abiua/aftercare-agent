import {expect} from '@playwright/test';
import {chatId, originalCaseId, signIn, test} from './api-fixture.mjs';

test('fixture: support reads investigation routes, actual references and model disagreements without raw input', async ({page, fixtureApi}) => {
  const event = (seq, kind, payload) => ({seq, kind: `investigation_${kind}`, payload, created_at: '2026-11-11T00:00:00Z'});
  fixtureApi.investigation = [
    {seq: 0, kind: 'decision_input', payload: {private: 'private-frozen-input'}, created_at: '2026-11-11T00:00:00Z'},
    event(1, 'route', {path: 'SINGLE', selected_path: 'STAGED', reason: 'SCOPED_STANDARD',
      financial_risk: {maximum_refundable_cents: 825, has_unsettled_refund: true},
      output: {messages: ['private-frozen-input']}}),
    event(2, 'input', {role: 'fact', phase_id: 'staged-fact', tools: ['permitted_only'], frozen_input: 'private-frozen-input'}),
    event(3, 'tool', {role: 'fact', phase_id: 'staged-fact', tool: 'read_order', evidence_ids: ['order-ref']}),
    event(8, 'claims', {role: 'policy', phase_id: 'staged-policy',
      claims: [{id: 'policy/claim', value: '政策角色提出的判断', state: 'UNRESOLVED'}]}),
    event(4, 'assessment', {recheck_count: 0, claims: [{id: 'fact/line', value: '复查前的判断'}, {id: 'fact/kept', value: '保留的判断'}]}),
    event(5, 'recheck', {target_role: 'fact', claim_ids: ['fact/line'], stale_claim_ids: ['policy/dependent'],
      tool: 'read_order_lines', evidence_ids: ['order-recheck']}),
    event(6, 'assessment', {recheck_count: 1, claims: [{id: 'fact/line', value: '复查后的判断'}, {id: 'fact/kept', value: '保留的判断'}],
      stale_claim_ids: ['policy/dependent']}),
    event(7, 'result', {claims: [{id: 'fact/line', role: 'fact', value: '助手提出的订单判断',
      state: 'CONTRADICTED', conflicting_refs: ['counter-ref']}],
    assessments: [{claim_id: 'fact/line', contradicted_field: 'status', verdict: 'CONTRADICTED', evidence_refs: ['counter-ref']}],
    selected_option_ids: ['support-option'], proposed_options: [{id: 'support-option', action: 'REFUND'}, {id: 'excluded-option', action: 'REPLACE'}]}),
  ];
  await signIn(page, 'support');
  await page.goto(`/?case=${originalCaseId}`);
  await page.getByRole('tab', {name: '调查过程与运行记录'}).click();
  await expect(page.getByText('单助手分阶段调查', {exact: false})).toBeVisible();
  await expect(page.getByText(/记录的可退款上限 · ¥8\.25/)).toBeVisible();
  await expect(page.getByText('返回的记录编号 · order-ref')).toBeVisible();
  await expect(page.getByText('复查启动时标为待重新确认 · policy/dependent')).toBeVisible();
  await expect(page.getByText('与复查前一致的判断 · fact/kept')).toBeVisible();
  await expect(page.getByText('复查后有修改的判断 · fact/line')).toBeVisible();
  await expect(page.getByText('未保留的候选 · excluded-option')).toBeVisible();
  await expect(page.getByText('反证记录 · counter-ref')).toBeVisible();
  await expect(page.getByText('助手提出的订单判断')).toBeVisible();
  await expect(page.getByText('政策角色提出的判断', {exact: false})).toBeVisible();
  await expect(page.getByText('private-frozen-input', {exact: false})).toHaveCount(0);
  await expect(page.getByText(/decision_input/)).toHaveCount(0);

  await page.getByRole('button', {name: '退出'}).click();
  await signIn(page, 'customer');
  await page.goto(`/?case=${originalCaseId}`);
  await expect(page.getByRole('tab', {name: '调查过程与运行记录'})).toHaveCount(0);
  expect(fixtureApi.calls.some((entry) => entry.role === 'customer' && entry.path.includes('/runs'))).toBe(false);
});

test('fixture: customer only sees durable investigation state and active evidence prompt', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  fixtureApi.chatInvestigationProgress = {state: 'CHECKING', internal: 'private-customer-projection'};
  fixtureApi.chatAnalysis = {issue_id: 'issue-a', generation: 1, state_version: 3, state: 'WAITING_CUSTOMER',
    pending_uploads: 0, wait_until: null, input_through_seq: 1};
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByRole('status').getByText('等待补充信息。')).toBeVisible();
  await expect(page.getByText('正在核对售后事项。')).toHaveCount(0);
  fixtureApi.chatInvestigationProgress = {state: 'PENDING_SUPPORT', internal: 'private-customer-projection'};
  await page.getByRole('button', {name: '刷新消息'}).click();
  await expect(page.getByText('已整理好调查结果，等待客服核对。')).toBeVisible();
  await expect(page.getByText('private-customer-projection', {exact: false})).toHaveCount(0);
  fixtureApi.chatInvestigationProgress = null;
  await page.getByRole('button', {name: '刷新消息'}).click();
  await expect(page.getByText('已整理好调查结果，等待客服核对。')).toHaveCount(0);
  expect(fixtureApi.calls.some((entry) => entry.role === 'customer' && entry.path.includes('/runs'))).toBe(false);
});
