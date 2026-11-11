import { expect } from '@playwright/test';
import { scenarioExperimentId, signIn, test } from './api-fixture.mjs';

const experimentUrl = `/?eval=experiments&eval_experiment=${scenarioExperimentId}`;

test('fixture: backend scope changes denominator, contribution opens exact repetition, and deep link restores', async ({page, fixtureApi}) => {
  fixtureApi.scenarioEnabled = true;
  await signIn(page, 'tester');
  await page.goto(experimentUrl);
  const experiment = page.getByRole('dialog', {name: '受控场景指标实验'});
  const metric = experiment.getByRole('row').filter({hasText: '任务成功率'});
  await expect(metric).toContainText('分母 3 次运行');
  await experiment.getByRole('combobox', {name: '指标场景'}).click();
  await page.locator('.ant-select-dropdown:visible').getByText('照片补证', {exact: true}).click();
  await expect(metric).toContainText('分母 2 次运行');
  await expect(metric).toContainText('50.0%');
  await expect(metric).toContainText('未确定 0 次运行');
  await expect(metric).not.toContainText('也可能已计入');
  const unknownCheck = experiment.getByRole('row').filter({hasText: '程序检查未确定项'});
  await expect(unknownCheck).toContainText('分母 2 项检查');
  await expect(unknownCheck).toContainText('未确定 1 项检查');
  await expect(unknownCheck).toContainText('未确定项也可能已计入样本和分母。');
  await experiment.getByRole('button', {name: '查看任务成功率的计入案例'}).click();
  await expect(experiment.getByRole('button', {name: 'AC02-C002 · 第 1 次'})).toHaveCount(0);
  await experiment.getByRole('combobox', {name: '案例计入方式'}).click();
  await page.locator('.ant-select-dropdown:visible').getByText('需关注', {exact: true}).click();
  await expect(experiment.getByRole('button', {name: 'AC02-C001 · 第 1 次'})).toHaveCount(0);
  const timedOut = experiment.getByRole('row').filter({hasText: 'AC02-C001 · 第 3 次'});
  await expect(timedOut).toContainText('计入统计');
  await expect(timedOut).toContainText('分母计入 1');
  await timedOut.getByRole('button', {name: 'AC02-C001 · 第 3 次'}).click();
  await expect(page.getByRole('dialog', {name: '案例 AC02-C001 · 第 3 次'})).toBeVisible();
  await expect(page).toHaveURL((url) => url.searchParams.get('eval_repeat') === '3'
    && url.searchParams.get('eval_scenario') === 'D_PHOTO' && url.searchParams.get('eval_metric') === 'task_success');

  await page.reload();
  const result = page.getByRole('dialog', {name: '案例 AC02-C001 · 第 3 次'});
  await expect(result).toBeVisible();
  await result.getByRole('button', {name: /^(关闭|Close)$/}).click();
  await expect(experiment.getByRole('button', {name: 'AC02-C001 · 第 3 次'})).toBeVisible();
  await expect(metric).toContainText('分母 2 次运行');
  expect(fixtureApi.calls.every(({method}) => method === 'GET')).toBe(true);
});

test('fixture: metric basis conflict refreshes summary before reading contributors again', async ({page, fixtureApi}) => {
  fixtureApi.scenarioEnabled = true;
  fixtureApi.scenarioConflict = true;
  await signIn(page, 'tester');
  await page.goto(experimentUrl + '&eval_scenario=D_PHOTO&eval_metric=task_success');
  await expect(page.getByText('统计依据有更新', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'AC02-C001 · 第 3 次'})).toBeVisible();
  expect(fixtureApi.calls.filter(({path}) => path.includes('/scenario-metrics')).map(({path, search}) => [
    path.endsWith('/contributors'), new URLSearchParams(search).get('basis_revision'),
  ])).toEqual([[false, null], [true, '1'.repeat(64)], [false, null], [true, '2'.repeat(64)]]);
  expect(fixtureApi.calls.filter(({path}) => path.includes('/scenario-metrics')).every(({search}) =>
    new URLSearchParams(search).get('scenario') === 'D_PHOTO')).toBe(true);
});

test('fixture: a backend without scenario capability keeps overall metrics and makes no scenario request', async ({page, fixtureApi}) => {
  await signIn(page, 'tester');
  await page.goto(experimentUrl + '&eval_scenario=D_PHOTO&eval_metric=task_success');
  await expect(page.getByText('整体能力指标', {exact: true})).toBeVisible();
  await expect(page.getByRole('combobox', {name: '指标场景'})).toHaveCount(0);
  expect(fixtureApi.calls.some(({path}) => path.includes('/scenario-metrics'))).toBe(false);
});


test('fixture: a late scope response cannot overwrite the selected scope or leak into another experiment', async ({page, fixtureApi}) => {
  fixtureApi.scenarioEnabled = true;
  fixtureApi.holdPhotoScope = true;
  await signIn(page, 'tester');
  await page.goto(experimentUrl);
  const experiment = page.getByRole('dialog', {name: '受控场景指标实验'});
  const metric = experiment.getByRole('row').filter({hasText: '任务成功率'});
  const scope = experiment.getByRole('combobox', {name: '指标场景'});
  const selectScope = async (label) => {
    await scope.click();
    await page.locator('.ant-select-dropdown:visible').getByText(label, {exact: true}).click();
  };
  try {
    await expect(metric).toContainText('分母 3 次运行');
    await selectScope('照片补证');
    await expect.poll(() => fixtureApi.calls.some(({search}) => new URLSearchParams(search).get('scenario') === 'D_PHOTO')).toBe(true);
    await selectScope('退款回执');
    await expect(metric).toContainText('分母 1 次运行');
  } finally {
    fixtureApi.releaseScenario();
  }
  await expect.poll(() => fixtureApi.releasedPhotoScope).toBe(true);
  await expect(metric).toContainText('分母 1 次运行');
  await expect(metric).toContainText('100.0%');

  await experiment.getByRole('button', {name: /^(关闭|Close)$/}).click();
  await page.getByRole('button', {name: '受控逐项复核实验'}).click();
  const next = page.getByRole('dialog', {name: '受控逐项复核实验'});
  await expect(next.getByRole('row').filter({hasText: '任务成功率'})).toContainText('分母 1 次运行');
  await expect(page).toHaveURL((url) => !url.searchParams.has('eval_scenario'));
  await next.getByRole('combobox', {name: '指标场景'}).click();
  const options = page.locator('.ant-select-dropdown:visible');
  await expect(options.getByText('退款回执', {exact: true})).toBeVisible();
  await expect(options.getByText('照片补证', {exact: true})).toHaveCount(0);
});


test('fixture: an unregistered historical definition shows a clear limit and retains the overall view', async ({page, fixtureApi}) => {
  fixtureApi.scenarioEnabled = true;
  fixtureApi.scenarioHistorical = true;
  await signIn(page, 'tester');
  await page.goto(experimentUrl + '&eval_metric=task_success');
  await expect(page.getByText('这份历史实验的指标定义暂无法重算', {exact: true})).toBeVisible();
  await expect(page.getByText('整体能力指标', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '查看任务成功率的计入案例'})).toHaveCount(0);
  expect(fixtureApi.calls.some(({path}) => path.endsWith('/contributors'))).toBe(false);
});
