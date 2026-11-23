import {expect} from '@playwright/test';
import {chatId, signIn, test} from './api-fixture.mjs';

test('fixture: only support associates a chat original with a manually chosen material type', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  fixtureApi.chatDecision = {conversation: {active_case_id: 'bridge-case'}};
  fixtureApi.materialBridge = {version: 2, posts: [], items: [{attachment_id: 'bridge-photo',
    message_id: 'bridge-message', sha256: 'a'.repeat(64), content_type: 'image/png', size: 123,
    suggested_kind: 'LABEL', material_id: null, confirmed_kind: null}]};
  await signIn(page, 'support');
  await page.goto(`/?conversation=${chatId}`);
  await page.getByRole('button', {name: '查看可关联照片'}).click();
  await page.getByRole('button', {name: '核对这张照片'}).click();
  const dialog = page.getByRole('dialog', {name: '确认照片关联'});
  await expect(dialog.getByText(/照片观察建议 · 包裹标签照片/)).toBeVisible();
  await expect(dialog.getByRole('button', {name: '关联到工单'})).toBeDisabled();
  await dialog.getByRole('radio', {name: '商品照片', exact: true}).check();
  await expect(dialog.getByRole('button', {name: '关联到工单'})).toBeDisabled();
  await dialog.getByRole('button', {name: '查看照片 · 聊天原图'}).click();
  const preview = page.getByRole('dialog', {name: '照片预览 · 聊天原图'});
  await expect(preview.getByRole('img', {name: '原图 · 聊天原图', exact: true})).toHaveAttribute('src', /^blob:/);
  await expect(preview.getByText(/原图 1 × 1 像素/)).toBeVisible();
  await preview.getByRole('button', {name: '关闭预览'}).press('Enter');
  await expect(preview).toBeHidden();
  await dialog.getByRole('button', {name: '关联到工单'}).click();
  await expect(page.getByText('已关联 · 商品照片')).toBeVisible();
  await expect(page.getByText('已关联工单。请到工单材料页完成审核。')).toBeVisible();
  expect(fixtureApi.materialBridge.posts).toEqual([{key: expect.any(String), body: {
    attachment_id: 'bridge-photo', kind: 'ITEM', expected_issue_id: 'bridge-issue',
    expected_case_id: 'bridge-case', expected_materials_version: 2,
  }}]);

  await page.getByRole('button', {name: '退出'}).click();
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByRole('button', {name: '查看可关联照片'})).toHaveCount(0);
  expect(fixtureApi.calls.some((call) => call.role === 'customer' && call.path.includes('decision-materials'))).toBe(false);
});
