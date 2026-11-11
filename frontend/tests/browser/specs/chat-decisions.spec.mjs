import {expect} from '@playwright/test';
import {chatId, originalCaseId, signIn, test} from './api-fixture.mjs';

test('fixture: support modification preserves the original and customer responds to the public version', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  const option = {id: 'option-1', action: 'REFUND', lines: [{line_id: 'L01', amount_cents: 1200}],
    rationale: '内部候选依据', evidence_ids: ['internal-photo-id'], policy_ids: ['POL01']};
  const original = {id: 'candidate-1', version: 1, sha256: 'a'.repeat(64), parent_id: null, origin: 'AGENT',
    input: {order_id: 'A1001', input_through_seq: 2}, candidate: {options: [option], unresolved: []},
    checks: {[option.id]: {status: 'PASSED'}}, expires_at: '2027-01-01T00:00:00Z'};
  fixtureApi.chatDecision = {original, current: original};
  await signIn(page, 'support');
  await page.getByRole('button', {name: '客户对话', exact: true}).click();
  await page.locator('.chat-list').getByRole('button').first().click();
  await expect(page.getByRole('heading', {name: /Agent 原始候选/})).toBeVisible();
  await page.getByRole('button', {name: '修改', exact: true}).click();
  await page.getByRole('textbox', {name: /退款金额/}).fill('8.25');
  await page.getByRole('textbox', {name: '客服复核理由'}).fill('内部客服核对说明');
  await page.getByRole('button', {name: '保存复核'}).click();
  await expect(page.getByRole('heading', {name: /客服修改方案/})).toBeVisible();
  expect(fixtureApi.chatDecisionPosts[0]).toMatchObject({role: 'support', key: expect.any(String), body: {
    ref: {id: original.id, version: 1, sha256: original.sha256}, expected_review_id: null,
    review: {action: 'MODIFY', reason: '内部客服核对说明', replacement: {lines: [{line_id: 'L01', amount_cents: 825}]}, resolved_items: []},
  }});
  await page.getByRole('button', {name: '查看修改前版本'}).click();
  await expect(page.getByRole('heading', {name: /Agent 原始候选/})).toBeVisible();
  await expect(page.getByText(/12\.00/)).toBeVisible();

  await page.getByRole('button', {name: '退出'}).click();
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByText('客服方案', {exact: true})).toBeVisible();
  await expect(page.getByText(/8\.25/)).toBeVisible();
  await expect(page.getByText(/内部客服核对说明|内部候选依据|internal-photo-id|启动方案调查/)).toHaveCount(0);
  await page.getByRole('button', {name: '接受方案'}).click();
  await page.getByRole('button', {name: '确认提交'}).click();
  await expect(page.getByText('已提交 · 接受方案')).toBeVisible();
  expect(fixtureApi.chatDecisionPosts[1]).toMatchObject({role: 'customer', key: expect.any(String), body: {
    ref: {id: 'offer-1', version: 1, sha256: 'c'.repeat(64)}, response: {action: 'ACCEPT'},
  }});
  expect(fixtureApi.calls.filter(({path, role}) => role === 'customer' && path.includes('/decisions')).every(({path}) => path.includes('/decisions/offer'))).toBe(true);
  await page.reload();
  await expect(page.getByText('已提交 · 接受方案')).toBeVisible();
  await expect(page.getByRole('button', {name: '接受方案', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: '撤回同意'})).toBeVisible();
  await page.getByRole('button', {name: '提出修改诉求'}).click();
  const change = page.getByRole('dialog', {name: '提出修改诉求'});
  await expect(change.getByText(/提交后会撤回之前的同意/)).toBeVisible();
  await change.getByRole('textbox', {name: '给客服的说明'}).fill('希望调整处理金额。');
  await change.getByRole('button', {name: '确认提交'}).click();
  await expect(page.getByText('已提交 · 提出修改诉求')).toBeVisible();
  expect(fixtureApi.chatDecisionPosts[2]).toMatchObject({role: 'customer', body: {
    ref: {id: 'offer-1', version: 1, sha256: 'c'.repeat(64)}, response: {action: 'REQUEST_CHANGE', reason: '希望调整处理金额。'},
  }});
  await page.reload();
  await expect(page.getByText('已提交 · 提出修改诉求')).toBeVisible();
  await expect(page.getByRole('button', {name: /接受方案|撤回同意|提出修改诉求/})).toHaveCount(0);
  await page.getByRole('button', {name: '退出'}).click();
  await signIn(page, 'support');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByRole('heading', {name: '客户对关联方案的回复'})).toBeVisible();
  await expect(page.getByText(/方案第 1 版 · 提出修改诉求/)).toBeVisible();
  await expect(page.getByText('希望调整处理金额。')).toBeVisible();
  await page.getByRole('button', {name: '要求补证', exact: true}).click();
  await page.getByRole('checkbox', {name: '包裹标签照片'}).check();
  await page.getByRole('textbox', {name: '客服复核理由'}).fill('需核对包裹标签。');
  await page.getByRole('button', {name: '保存复核'}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(fixtureApi.chatDecisionPosts[3].body).toMatchObject({evidence_gaps: ['LABEL'],
    review: {action: 'REQUEST_EVIDENCE', reason: '需核对包裹标签。'}});
});

test('fixture: support browses paginated automatic history read-only and ignores a late prior-issue result', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  const snapshot = (version) => ({id: `auto-${version}`, version, sha256: 'a'.repeat(64), parent_id: null,
    origin: 'AGENT', input: {order_id: 'A1001', input_through_seq: 2}, expires_at: '2027-01-01T00:00:00Z',
    checks: {option: {status: 'PASSED'}}, candidate: {unresolved: [], options: [{
      id: 'option', action: 'REFUND', lines: [{line_id: 'L01', amount_cents: 1200}],
      rationale: `自动候选 ${version} 的内部依据`, evidence_ids: ['photo'], policy_ids: ['POL01'],
    }]}});
  const [first, second, current] = [1, 2, 3].map(snapshot);
  fixtureApi.chatDecision = {current, history: [first, second, current], historyPageSize: 2};
  await signIn(page, 'support');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByRole('heading', {name: 'Agent 原始候选 · 第 3 版'})).toBeVisible();
  await page.getByRole('button', {name: '查看候选历史'}).click();
  await page.getByRole('button', {name: '第 2 版 · Agent 原始候选'}).click();
  await expect(page.getByText('自动候选 2 的内部依据')).toBeVisible();
  await expect(page.getByRole('button', {name: '修改', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: '启动方案调查'})).toHaveCount(0);
  await page.getByRole('button', {name: '更早的版本'}).click();
  await page.getByRole('button', {name: '第 1 版 · Agent 原始候选'}).click();
  await expect(page.getByText('自动候选 1 的内部依据')).toBeVisible();
  await page.getByRole('button', {name: '返回当前方案'}).click();
  await expect(page.getByRole('button', {name: '修改', exact: true})).toBeVisible();

  const {promise, resolve} = Promise.withResolvers();
  fixtureApi.chatDecision.holdHistory = {[first.id]: promise};
  const delayed = page.waitForRequest((request) => request.url().endsWith('/history/auto-1'));
  await page.getByRole('button', {name: '第 1 版 · Agent 原始候选'}).click();
  await delayed;
  try {
    Object.assign(fixtureApi.chatDecision, {issueId: 'new-issue', current: snapshot(4),
      conversation: {revision: 2}});
    await page.getByRole('button', {name: '刷新消息'}).click();
    await expect(page.getByRole('heading', {name: 'Agent 原始候选 · 第 4 版'})).toBeVisible();
  } finally {
    resolve();
  }
  await expect(page.getByText('自动候选 1 的内部依据')).toHaveCount(0);
  await expect(page.getByRole('button', {name: '返回当前方案'})).toHaveCount(0);
  expect(fixtureApi.chatDecisionPosts).toHaveLength(0);
});

test('fixture: scope change removes the previous offer while the new offer read is pending', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  const offer = {id: 'offer-old', version: 1, sha256: 'd'.repeat(64), action: 'REFUND',
    lines: [{line_id: 'L01', amount_cents: 912}], policy_ids: ['POL01'],
    expires_at: '2027-01-01T00:00:00Z', customer_decision: null};
  fixtureApi.chatDecision = {offer};
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByText(/9\.12/)).toBeVisible();
  await page.getByRole('button', {name: '接受方案'}).click();
  await expect(page.getByRole('dialog')).toBeVisible();

  const {promise, resolve} = Promise.withResolvers();
  Object.assign(fixtureApi.chatDecision, {offer: null, holdOffer: promise,
    conversation: {active_order_id: 'A1002', revision: 2}});
  try {
    await expect(page.locator('.chat-thread').getByText('订单 A1002', {exact: true})).toBeVisible({timeout: 8_000});
    await expect(page.getByText(/9\.12/)).toHaveCount(0);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(fixtureApi.chatDecisionPosts).toHaveLength(0);
  } finally {
    resolve();
  }
});

test('fixture: a failed recheck on a previously checked candidate is visible to support', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  const option = {id: 'option-1', action: 'REFUND', lines: [{line_id: 'L01', amount_cents: 1200}],
    rationale: '内部候选依据', evidence_ids: ['internal-photo-id'], policy_ids: ['POL01']};
  const original = {id: 'candidate-1', version: 1, sha256: 'a'.repeat(64), parent_id: null, origin: 'AGENT',
    input: {order_id: 'A1001', input_through_seq: 2}, candidate: {options: [option], unresolved: []},
    checks: {[option.id]: {status: 'PASSED'}}, expires_at: '2027-01-01T00:00:00Z'};
  fixtureApi.chatDecision = {original, current: original, blockedReview: true};
  await signIn(page, 'support');
  await page.goto(`/?conversation=${chatId}`);
  await page.getByRole('button', {name: '采纳', exact: true}).click();
  await page.getByRole('textbox', {name: '客服复核理由'}).fill('采用前重新核对金额。');
  await page.getByRole('button', {name: '保存复核'}).click();
  await expect(page.getByText('复核已记录，暂未向客户发送方案。')).toBeVisible();
  expect(fixtureApi.chatDecisionPosts[0].body.review.action).toBe('ACCEPT');
  await page.reload();
  await expect(page.getByText('候选生成时核对通过')).toBeVisible();
  await expect(page.getByText('复核已记录，暂未向客户发送方案。')).toBeVisible();
  await expect(page.getByText('方案包含当前售后事项无法使用的材料。')).toBeVisible();
});

test('fixture: customer sees current case simulated ledger progress and no success before a receipt', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  const executionProgress = {id: 'execution-1', case_id: 'active-case', action: 'REFUND',
    status: 'SUBMITTING', receipt_confirmed: false, simulated: true, amount_cents: 825,
    lines: [{line_id: 'L01', amount_cents: 825}], updated_at: '2026-11-11T00:00:00Z'};
  fixtureApi.chatDecision = {conversation: {active_case_id: 'active-case'}, executionProgress};
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByText('模拟退款 · 等待回执')).toBeVisible();
  expect(fixtureApi.chatDecisionPosts).toHaveLength(0);
  executionProgress.status = 'SUCCEEDED';
  await page.getByRole('button', {name: '刷新消息'}).click();
  await expect(page.getByText('模拟退款 · 结果待核对')).toBeVisible();
  executionProgress.receipt_confirmed = true;
  await page.getByRole('button', {name: '刷新消息'}).click();
  await expect(page.getByText('模拟退款 · 执行成功')).toBeVisible();
  await expect(page.getByText(/执行编号 execution-1/)).toBeVisible();

  fixtureApi.chatDecision.conversation = {active_case_id: 'other-case', revision: 2};
  await page.getByRole('button', {name: '刷新消息'}).click();
  await expect(page.getByText('本次售后执行进度')).toHaveCount(0);
});

function seedHandoff(state) {
  const reference = {id: 'current-offer', version: 2, sha256: 'b'.repeat(64)};
  const offer = {...reference, action: 'REFUND', lines: [{line_id: 'L01', amount_cents: 825}],
    policy_ids: ['POL01'], expires_at: '2099-01-01T00:00:00Z'};
  state.chatCreated = true;
  state.chatDecision = {
    current: {id: 'current-candidate', version: 3, sha256: 'a'.repeat(64), origin: 'AGENT', parent_id: null,
      input: {case_id: originalCaseId, order_id: 'A1001', input_through_seq: 2}, candidate: {options: [], unresolved: []},
      checks: {}, expires_at: '2099-01-01T00:00:00Z'},
    conversation: {active_case_id: originalCaseId},
    customerDecision: {id: 'accepted-decision', action: 'ACCEPT', offer: reference, created_at: '2026-11-11T00:00:00Z'},
  };
  state.proposalHandoff = {case_id: originalCaseId, conversation_id: chatId, issue_id: 'decision-issue',
    status: 'READY', offer};
  return {reference, offer};
}

test('fixture: support hands off confirmed chat terms and submits the exact frozen offer reference', async ({page, fixtureApi}) => {
  const {reference} = seedHandoff(fixtureApi);
  await signIn(page, 'support');
  await page.goto(`/?conversation=${chatId}`);
  await page.getByRole('button', {name: '到工单提交已确认方案'}).click();
  await expect(page.getByRole('tab', {name: '方案与回执'})).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('button', {name: '核对已确认方案并提交'}).click();
  const modal = page.getByRole('dialog', {name: '提交处理方案'});
  await expect(modal.getByText('商品行 L01 · ¥8.25')).toBeVisible();
  await expect(modal.getByText('提交后仍需独立审批。', {exact: true})).toBeVisible();
  await expect(modal.getByRole('combobox')).toHaveCount(0);
  await modal.getByRole('textbox', {name: '处理说明'}).fill('按已确认的订单行办理。');
  await modal.getByRole('button', {name: '提交处理方案'}).click();
  await expect(modal).toHaveCount(0);
  expect(fixtureApi.calls.filter(({method, path}) => method === 'POST' && path.endsWith('/proposals'))).toMatchObject([{
    body: {action: 'REFUND', lines: [{line_id: 'L01', amount_cents: 825}], policy_ids: ['POL01'],
      notes: '按已确认的订单行办理。', expected_offer: reference},
  }]);
});

test('fixture: an identical replacement offer invalidates handoff and waiting customer cannot propose', async ({page, fixtureApi}) => {
  const {offer} = seedHandoff(fixtureApi);
  await signIn(page, 'support');
  await page.goto(`/?conversation=${chatId}`);
  await page.getByRole('button', {name: '到工单提交已确认方案'}).click();
  await page.getByRole('button', {name: '核对已确认方案并提交'}).click();
  const modal = page.getByRole('dialog', {name: '提交处理方案'});
  fixtureApi.proposalHandoff.offer = {...offer, id: 'replacement-offer', version: 3};
  await expect(modal.getByText('工单状态已更新，请关闭后核对最新处理记录。')).toBeVisible({timeout: 8_000});
  await expect(modal.getByRole('button', {name: '提交处理方案'})).toBeDisabled();
  await modal.getByRole('button', {name: '关闭'}).click();
  await expect(page.getByText('聊天中的方案已变化或无法核实，请返回聊天重新打开当前方案。')).toBeVisible();

  Object.assign(fixtureApi.proposalHandoff, {status: 'WAITING_CUSTOMER', offer: null});
  await page.goto(`/?case=${originalCaseId}`);
  await page.getByRole('tab', {name: '方案与回执'}).click();
  await expect(page.getByText('等待客户确认聊天中的处理建议。')).toBeVisible();
  await expect(page.getByRole('button', {name: /新建处理方案|核对已确认方案并提交/})).toHaveCount(0);
  expect(fixtureApi.calls.filter(({method, path}) => method === 'POST' && path.endsWith('/proposals'))).toHaveLength(0);
});
