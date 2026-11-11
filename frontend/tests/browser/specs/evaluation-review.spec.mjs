import { expect } from '@playwright/test';
import { reviewExperimentId, signIn, test } from './api-fixture.mjs';

async function openReview(page) {
  await signIn(page, 'tester');
  await page.goto(`/?eval=experiments&eval_experiment=${reviewExperimentId}&eval_case=AC02-C001&eval_repeat=1`);
  const dialog = page.getByRole('dialog', {name: '案例 AC02-C001 · 第 1 次'});
  await dialog.getByRole('button', {name: '添加人工复核'}).click();
  return dialog;
}

async function fillOverall(page, dialog, notes, decision = '通过') {
  await dialog.getByRole('combobox', {name: '复核结论'}).click();
  await page.locator('.ant-select-dropdown:visible').getByText(decision, {exact: true}).click();
  await dialog.getByRole('textbox', {name: '证据与说明'}).fill(notes);
}

test('fixture: saves exact explicitly chosen keys and confirms replacing current labels with a new revision', async ({page, fixtureApi}) => {
  const dialog = await openReview(page);
  const tool = dialog.getByRole('radiogroup', {name: /第 1 次查询/});
  const citation = dialog.getByRole('radiogroup', {name: '第 1 条引用的判断'});
  const fact = dialog.getByRole('radiogroup', {name: '照片审核结果的判断'});
  for (const group of [tool, citation, fact]) {
    await expect(group.getByRole('radio', {name: '暂不评'})).toBeChecked();
  }
  await expect(dialog.getByText('照片审核通过', {exact: true})).toBeVisible();
  await expect(dialog.getByText('验收用示例条款原文。', {exact: true})).toBeVisible();
  await tool.getByRole('radio', {name: '无法判断'}).check();
  await citation.getByRole('radio', {name: '不正确', exact: true}).check();
  await fillOverall(page, dialog, '  受控测试意见  ');
  await dialog.getByRole('button', {name: '保存复核'}).click();
  await expect(dialog.getByText('受控测试意见', {exact: true})).toBeVisible();
  expect(fixtureApi.reviewCalls).toEqual([{
    key: expect.any(String),
    body: {expected_version: 1, decision: 'PASS', notes: '受控测试意见',
      annotations: {tools: {'0': 'UNKNOWN'}, citations: {'POL06@1.0': 'FAIL'}, facts: {}}},
  }]);
  await dialog.getByText('查看该版逐项标注', {exact: true}).click();
  await expect(dialog.getByText('不正确', {exact: true})).toBeVisible();

  await dialog.getByRole('button', {name: '添加人工复核'}).click();
  await expect(tool.getByRole('radio', {name: '暂不评'})).toBeChecked();
  await expect(citation.getByRole('radio', {name: '暂不评'})).toBeChecked();
  await fillOverall(page, dialog, '本次仅提供总评。', '无法判断');
  await expect(dialog.getByText('本次总评为无法判断，所选逐项标注只保留在历史中。')).toBeVisible();
  await dialog.getByRole('button', {name: '保存复核'}).click();
  await expect(dialog.getByText('请确认新版本的替换范围')).toBeVisible();
  expect(fixtureApi.reviewCalls).toHaveLength(1);

  await dialog.getByRole('checkbox', {name: '确认按本次选择保存'}).check();
  await dialog.getByRole('button', {name: '保存复核'}).click();
  await expect(dialog.getByText('未提交逐项标注', {exact: true})).toBeVisible();
  expect(fixtureApi.reviewCalls[1].body).toEqual({expected_version: 2, decision: 'UNKNOWN', notes: '本次仅提供总评。'});
  expect(fixtureApi.reviewHistory[0].annotations.citations).toEqual({'POL06@1.0': 'FAIL'});
});

test('fixture: version conflict keeps prior choices local and requires opening a fresh review', async ({page, fixtureApi}) => {
  fixtureApi.reviewConflict = true;
  const dialog = await openReview(page);
  const citation = dialog.getByRole('radiogroup', {name: '第 1 条引用的判断'});
  await citation.getByRole('radio', {name: '正确', exact: true}).check();
  await fillOverall(page, dialog, '先前浏览时的意见');
  await dialog.getByRole('button', {name: '保存复核'}).click();
  await expect(dialog.getByText('另一位复核员已保存。')).toBeVisible();
  await expect(dialog.getByText('案例结果或复核记录已更新，请取消编辑并核对最新记录。')).toBeVisible();
  await expect(dialog.getByRole('button', {name: '保存复核'})).toBeDisabled();
  expect(fixtureApi.reviewCalls).toHaveLength(1);
  expect(fixtureApi.reviewCalls[0].body.expected_version).toBe(1);

  await dialog.getByRole('button', {name: '取消编辑'}).click();
  await dialog.getByRole('button', {name: '添加人工复核'}).click();
  await expect(citation.getByRole('radio', {name: '暂不评'})).toBeChecked();
  await fillOverall(page, dialog, '查阅最新记录后另写的意见。');
  await dialog.getByRole('button', {name: '保存复核'}).click();
  await expect(dialog.getByText('查阅最新记录后另写的意见。', {exact: true})).toBeVisible();
  expect(fixtureApi.reviewCalls.map(({body}) => body.expected_version)).toEqual([1, 2]);
  expect(fixtureApi.reviewCalls[1].key).not.toBe(fixtureApi.reviewCalls[0].key);
});

test('fixture: legacy results with no audit keys can save overall review only', async ({page, fixtureApi}) => {
  fixtureApi.reviewLegacy = true;
  const dialog = await openReview(page);
  await expect(dialog.getByText('历史报告缺少引用核对记录。')).toBeVisible();
  await expect(dialog.getByRole('radiogroup')).toHaveCount(0);
  await fillOverall(page, dialog, '旧版证据不足，无法判断。', '无法判断');
  await dialog.getByRole('button', {name: '保存复核'}).click();
  await expect(dialog.getByText('未提交逐项标注', {exact: true})).toBeVisible();
  expect(fixtureApi.reviewCalls[0].body).toEqual({
    expected_version: 1, decision: 'UNKNOWN', notes: '旧版证据不足，无法判断。',
  });
});


test('fixture: reads the customer question and ordered published stages before reviewing', async ({page, fixtureApi}) => {
  fixtureApi.reviewReading = true;
  await signIn(page, 'tester');
  await page.goto(`/?eval=experiments&eval_experiment=${reviewExperimentId}&eval_case=AC02-C001&eval_repeat=1`);
  const dialog = page.getByRole('dialog', {name: '案例 AC02-C001 · 第 1 次'});
  await expect(dialog.getByRole('region', {name: '客户原话'})).toContainText('耳机发错了，我要拍哪几张？');
  const stages = dialog.getByRole('region', {name: '业务步骤与处理意见'});
  await expect(stages.getByRole('heading')).toHaveText([
    '业务步骤与应用记录的处理意见', '助手开始处理', '已上传包裹标签照片', '继续处理',
  ]);
  await expect(stages).toContainText('需补材料：错发商品实物照片、包裹标签照片');
  await expect(stages).toContainText('这一步没有发布处理意见。');
  await expect(stages).not.toContainText('内部笔记称退款已批准。');
  await expect(stages).not.toContainText('照片审核通过');
  await expect(dialog.getByRole('region', {name: '报告结束时的业务记录'})).toContainText('照片审核通过');
  await expect(dialog.getByText('内部笔记称退款已批准。', {exact: false})).toHaveCount(0);
  await dialog.getByText('查看内部草稿和笔记', {exact: true}).click();
  await expect(dialog.getByText(/内部笔记称退款已批准/)).toBeVisible();
  await expect(dialog.getByRole('combobox', {name: '复核结论'})).toHaveCount(0);
  expect(fixtureApi.reviewCalls).toEqual([]);
});
