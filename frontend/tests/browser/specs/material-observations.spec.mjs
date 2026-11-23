import {expect} from '@playwright/test';
import {originalCaseId, signIn, test} from './api-fixture.mjs';

async function openReview(page, fixtureApi) {
  fixtureApi.materialReview = {};
  await signIn(page, 'support', `/?case=${originalCaseId}`);
  await page.getByRole('tab', {name: '材料与审核'}).click();
  await page.getByRole('button', {name: '记录人工审核'}).click();
  return page.getByRole('dialog', {name: '记录人工审核'});
}

async function openOptions(page, control) {
  await control.click();
  await expect(control).toHaveAttribute('aria-expanded', 'true');
  const list = await control.getAttribute('aria-controls');
  return page.locator(`[id="${list}"]`);
}

async function choose(page, control, option) {
  const options = await openOptions(page, control);
  await options.getByRole('option', {name: option, exact: true}).click();
}

async function chooseApproved(dialog) {
  const verdict = dialog.getByRole('combobox', {name: '审核结论'});
  await verdict.press('ArrowDown');
  await expect(verdict).toHaveAttribute('aria-expanded', 'true');
  await verdict.press('Enter');
  await expect(verdict).toHaveAttribute('aria-expanded', 'false');
}

function reviewPosts(fixtureApi) {
  return fixtureApi.calls.filter(({path, method}) => method === 'POST' && path.endsWith('/material-reviews'));
}

test('fixture: a review without any photo observation keeps the legacy request', async ({page, fixtureApi}) => {
  const dialog = await openReview(page, fixtureApi);
  await chooseApproved(dialog);
  await dialog.getByRole('textbox', {name: '审核意见'}).fill('  原审核意见  ');
  await dialog.getByRole('button', {name: '保存审核结论'}).click();
  await expect(page.getByText('已记录本次人工审核。')).toBeVisible();
  expect(reviewPosts(fixtureApi).map(({body}) => body)).toEqual([
    {expected_version: 5, decision: 'APPROVED', notes: '原审核意见'},
  ]);
  await expect(page.getByRole('button', {name: '查看照片观察'})).toHaveCount(0);
});

test('fixture: scoped photo observations corroborate a line total without summing', async ({page, fixtureApi}) => {
  const dialog = await openReview(page, fixtureApi);
  await chooseApproved(dialog);
  await dialog.getByRole('textbox', {name: '审核意见'}).fill('合成材料核对。');
  await dialog.getByRole('button', {name: '照片观察（可选）'}).click();
  await dialog.getByRole('button', {name: '查看照片 1', exact: true}).click();
  const image = page.getByRole('dialog', {name: '商品照片'});
  await expect(image.getByRole('img', {name: 'fixture-proof.png'})).toHaveAttribute('src', /^blob:/);
  await image.getByRole('button', {name: /关闭|close/i}).click();

  for (const index of [1, 2]) {
    await choose(page, dialog.getByRole('combobox', {name: `照片 ${index} 的观察结果`}), '可见破损');
    const lines = dialog.getByRole('combobox', {name: `照片 ${index} 的对应商品行`});
    const options = await openOptions(page, lines);
    await expect(options.getByRole('option', {name: '示例背包 · L02'})).toHaveCount(0);
    await options.getByRole('option', {name: '示例外套 · L01'}).click();
  }
  const first = dialog.getByRole('textbox', {name: '照片 1 的该商品行受影响总数'});
  const second = dialog.getByRole('textbox', {name: '照片 2 的该商品行受影响总数'});
  const save = dialog.getByRole('button', {name: '保存审核结论'});
  await first.fill('1');
  await second.fill('3');
  await expect(dialog.getByText('示例外套 · L01 的受影响总数须为 1 至 2 的整数。')).toBeVisible();
  await expect(save).toBeDisabled();
  await second.fill('2');
  await expect(dialog.getByText('示例外套 · L01 在不同照片中填写的受影响总数须一致。')).toBeVisible();
  await expect(save).toBeDisabled();
  await second.fill('1');
  await expect(save).toBeEnabled();
  await save.click();

  await expect(page.getByText('已记录本次人工审核。')).toBeVisible();
  expect(reviewPosts(fixtureApi).map(({body}) => body)).toEqual([{
    expected_version: 5, decision: 'APPROVED', notes: '合成材料核对。',
    observations: [
      {material_id: 'fixture-photo', finding: 'VISIBLE_DAMAGE', line_id: 'L01', affected_quantity: 1},
      {material_id: 'fixture-parcel', finding: 'VISIBLE_DAMAGE', line_id: 'L01', affected_quantity: 1},
    ],
  }]);
  await page.getByRole('button', {name: '查看照片观察'}).click();
  await expect(page.getByRole('listitem').filter({hasText: '该商品行受影响总数 1 件'})).toHaveCount(2);
});
