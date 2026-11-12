import { expect } from '@playwright/test';
import { financialCaseId, financialExecutionId, originalCaseId, signIn, test } from './api-fixture.mjs';

test('fixture: rejects invalid login and shows support-only workbench entry', async ({page, fixtureApi}) => {
  await page.goto('/');
  fixtureApi.rejectNextLogin = true;
  const login = page.getByRole('button', {name: '以客服身份进入'});
  await login.click();
  await expect(page.getByText('登录凭据失效，请重新登录。')).toBeVisible();
  await login.click();
  await expect(page.getByText('测试客服', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '新建售后工单'})).toBeVisible();
  await expect(page.getByRole('button', {name: '描述问题发起售后'})).toHaveCount(0);
  await expect(page.getByRole('button', {name: '测试与评测'})).toHaveCount(0);
  expect(fixtureApi.calls.filter((item) => item.path === '/api/me').map((item) => item.role)).toEqual([undefined, 'support']);
  expect(fixtureApi.calls.filter((item) => item.path.startsWith('/api/intake/'))).toEqual([]);
});

test('fixture: customer deep links keep the legacy ownership boundary', async ({page, fixtureApi}) => {
  await signIn(page, 'customer');
  await expect(page.getByRole('button', {name: '测试与评测'})).toHaveCount(0);
  await page.goto('/?case=fixture-case-other');
  await expect(page.getByText('记录不存在，或当前身份无权查看。', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '上传照片'})).toHaveCount(0);
  expect(fixtureApi.calls.some((call) => call.path === '/api/cases/fixture-case-other' && call.role === 'customer')).toBe(true);
});

test('mock upload: customer constructs multipart material request in the browser', async ({page, fixtureApi}) => {
  await signIn(page, 'customer');
  await page.goto(`/?case=${originalCaseId}`);
  await page.getByRole('button', {name: '上传照片'}).click();
  const dialog = page.getByRole('dialog');
  const content = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3FYAAAAASUVORK5CYII=', 'base64');
  await dialog.locator('#material-file').setInputFiles({name: 'fixture-proof.png', mimeType: 'image/png', buffer: content});
  await dialog.getByRole('button', {name: '提交照片'}).click();
  await expect(page.getByText('照片已提交，当前审核状态请查看材料记录。', {exact: true})).toBeVisible();
  await expect(page.getByText('fixture-proof.png', {exact: true})).toBeVisible();
  expect(fixtureApi.uploads).toHaveLength(1);
  const [upload] = fixtureApi.uploads;
  expect(upload.contentType).toMatch(/^multipart\/form-data; boundary=/);
  expect(upload.body.includes(content)).toBe(true);
  const fields = upload.body.toString('utf8');
  expect(fields).toContain('name="kind"\r\n\r\nITEM');
  expect(fields).toContain('name="expected_version"\r\n\r\n1');
  expect(fields).toContain('name="file"; filename="fixture-proof.png"');
});

for (const [status, statusLabel] of [['UNKNOWN', '未知，待确认'], ['SUBMITTING', '正在提交']]) {
  test(`fixture: ${status} execution remains reconcile-only after page refresh`, async ({page, fixtureApi}) => {
    fixtureApi.executionStatus = status;
    await signIn(page, 'support');
    await page.goto(`/?case=${financialCaseId}`);
    await page.getByRole('tab', {name: '方案与回执'}).click();
    await expect(page.getByRole('button', {name: '核对执行结果'})).toBeVisible();

    await page.reload();
    await page.getByRole('tab', {name: '方案与回执'}).click();
    const pendingRow = page.getByRole('row').filter({hasText: statusLabel});
    await expect(pendingRow).toBeVisible();
    await expect(page.getByRole('button', {name: '模拟执行', exact: true})).toHaveCount(0);
    await pendingRow.getByRole('button', {name: '核对执行结果'}).click();
    await page.getByRole('dialog', {name: '核对执行结果'}).getByRole('button', {name: '核对执行结果'}).click();
    await expect(page.getByText('已办结', {exact: true}).first()).toBeVisible();
    await expect(page.getByRole('button', {name: '核对执行结果'})).toHaveCount(0);
    const posts = fixtureApi.calls.filter((item) => item.method === 'POST' && item.path.startsWith('/api/cases/'));
    expect(posts).toHaveLength(1);
    expect(posts[0]).toEqual({
      path: `/api/cases/${financialCaseId}/executions/${financialExecutionId}/reconcile`,
      method: 'POST', role: 'support', body: {expected_version: 7}, key: expect.any(String),
    });
  });

  test(`fixture: an expired approval cannot be renewed after a ${status} execution`, async ({page, fixtureApi}) => {
    fixtureApi.approvalExpired = true;
    await signIn(page, 'approver');
    await page.goto(`/?case=${financialCaseId}`);
    await page.getByRole('tab', {name: '财务审批与依据'}).click();
    await expect(page.getByRole('button', {name: '重新审批', exact: true})).toBeVisible();

    fixtureApi.executionStatus = status;
    await page.reload();
    await page.getByRole('tab', {name: '财务审批与依据'}).click();
    await expect(page.getByRole('row').filter({hasText: statusLabel})).toBeVisible();
    await expect(page.getByRole('button', {name: /^(重新审批|审批|审核财务方案|模拟执行)$/})).toHaveCount(0);
    expect(fixtureApi.calls.filter((item) => item.method === 'POST' && item.path.startsWith('/api/cases/'))).toEqual([]);
  });
}

test('fixture: a held execution submits once and both manual retries preserve the same request', async ({page, fixtureApi}) => {
  await signIn(page, 'support');
  await page.goto(`/?case=${financialCaseId}`);
  await page.getByRole('tab', {name: '方案与回执'}).click();
  await page.getByRole('button', {name: '模拟执行', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: '确认模拟执行'});
  await dialog.getByRole('checkbox', {name: '我已核对已审批的方案，确认发起模拟执行'}).check();
  const form = dialog.locator('form');
  const submit = dialog.getByRole('button', {name: '确认模拟执行'});

  try {
    await form.evaluate((element) => {
      element.requestSubmit();
      element.requestSubmit();
    });
    await expect.poll(() => fixtureApi.executions.length).toBe(1);
    await form.evaluate(async (element) => {
      element.requestSubmit();
      await new Promise((resolve) => element.ownerDocument.defaultView.requestAnimationFrame(resolve));
    });
    expect(fixtureApi.executions).toHaveLength(1);
  } finally {
    fixtureApi.releaseExecution();
  }

  await expect(dialog.getByText(/当前表单保持打开且提交内容相同时，重试会沿用上一次请求编号/)).toBeVisible();
  expect(fixtureApi.executions).toHaveLength(1);
  await submit.click();
  await expect(dialog.getByText('受控限流，请稍后重试。')).toBeVisible();
  expect(fixtureApi.executions).toHaveLength(2);
  await submit.click();
  await expect(page.getByText('已办结', {exact: true}).first()).toBeVisible();
  expect(fixtureApi.executions).toHaveLength(3);
  expect(fixtureApi.executions[0]).toEqual({
    body: {expected_version: 6, proposal_id: 'fixture-approved-proposal'}, key: expect.any(String),
  });
  expect(fixtureApi.executions[1]).toEqual(fixtureApi.executions[0]);
  expect(fixtureApi.executions[2]).toEqual(fixtureApi.executions[0]);
});
