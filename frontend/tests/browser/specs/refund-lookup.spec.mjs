import { expect } from '@playwright/test';
import { refundLookupCaseId, refundLookupIds, signIn, test } from './api-fixture.mjs';

async function openLookup(page) {
  await signIn(page, 'customer');
  await page.goto(`/?case=${refundLookupCaseId}`);
  await page.getByRole('tab', {name: '方案与回执'}).click();
}

test('fixture: a single refund is never auto-selected; invalid input stays local and clear returns to the order', async ({page, fixtureApi}) => {
  fixtureApi.lookupSingle = true;
  await openLookup(page);
  const input = page.getByRole('textbox', {name: '退款执行编号'});
  const search = page.getByRole('button', {name: '查询退款执行记录'});
  await expect(input).toHaveValue('');
  await expect(page.getByText(/订单历史累计登记退款.*359.00/)).toBeVisible();
  expect(fixtureApi.refundLookupCalls).toEqual([]);

  await search.click();
  await expect(page.getByText('要查某一笔退款，请提供退款执行编号。')).toBeVisible();
  await input.fill('O0079');
  await search.click();
  await expect(page.getByText('请输入一个完整的退款执行编号。')).toBeVisible();
  expect(fixtureApi.refundLookupCalls).toEqual([]);

  await input.fill(`  ${refundLookupIds[0].toUpperCase()}  `);
  await search.click();
  const result = page.getByRole('alert').filter({hasText: '找到指定退款执行记录'});
  await expect(result).toContainText('模拟退款处理成功');
  await expect(result).toContainText('¥398.00');
  await expect(result).toContainText('本地模拟回执已确认');
  expect(fixtureApi.refundLookupCalls).toEqual([refundLookupIds[0]]);

  await page.getByRole('button', {name: '清空查询'}).click();
  await expect(input).toHaveValue('');
  await expect(result).toHaveCount(0);
  await expect(page.getByText(/订单历史累计登记退款.*359.00/)).toBeVisible();
  expect(fixtureApi.calls.every(({method}) => method === 'GET')).toBe(true);
});

test('fixture: explicit selection reads exactly one record; a late response cannot replace it', async ({page, fixtureApi}) => {
  fixtureApi.holdRefundLookup = true;
  await openLookup(page);
  const select = (id) => page.getByRole('button', {name: `查询退款执行编号 ${id}`});
  const result = page.getByRole('alert').filter({hasText: '找到指定退款执行记录'});
  try {
    await select(refundLookupIds[0]).click();
    await expect.poll(() => fixtureApi.refundLookupCalls).toEqual([refundLookupIds[0]]);
    await select(refundLookupIds[1]).click();
    await expect(result).toContainText(refundLookupIds[1]);
    await expect(result).toContainText('模拟退款结果待核对');
    await expect(result).toContainText('未确认模拟回执');
    await expect(result).not.toContainText('¥398.00');
  } finally {
    fixtureApi.releaseRefundLookup();
  }
  await expect.poll(() => fixtureApi.refundLookupReleased).toBe(true);
  await expect(result).toContainText(refundLookupIds[1]);

  await page.getByRole('textbox', {name: '退款执行编号'}).fill('df806279-78e7-4765-8a7d-cd3b05215de2');
  await expect(result).toHaveCount(0);
  await page.getByRole('button', {name: '查询退款执行记录'}).click();
  await expect(page.getByText('这份工单的订单中未找到该笔退款执行记录。请核对编号。')).toBeVisible();
  expect(fixtureApi.calls.every(({method}) => method === 'GET')).toBe(true);
});

test('fixture: older backend retains the order view without an unsupported lookup request', async ({page, fixtureApi}) => {
  fixtureApi.lookupLegacy = true;
  await openLookup(page);
  await expect(page.getByRole('heading', {name: '该订单的退款执行记录'})).toBeVisible();
  await expect(page.getByRole('textbox', {name: '退款执行编号'})).toHaveCount(0);
  expect(fixtureApi.refundLookupCalls).toEqual([]);
});
