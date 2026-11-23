import { readFile } from 'node:fs/promises';
import { expect } from '@playwright/test';
import { signIn, test } from './api-fixture.mjs';

test('official report keeps the planned denominator and distinguishes unfinished runs', async ({page, fixtureApi}) => {
  fixtureApi.officialReport = 'incomplete';
  await signIn(page, 'tester', '/?eval=experiments&eval_experiment=retired-run');
  await expect(page.getByRole('tab', {name: '官方 τ²-Bench'})).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', {name: /用例库|实验与能力指标|版本对照/})).toHaveCount(0);
  await expect(page.getByRole('columnheader', {name: '当前官方成功率'})).toBeVisible();

  const single = page.getByRole('row').filter({has: page.getByRole('cell', {name: '50.0%（1/2）', exact: true})})
    .filter({hasText: '强单 Agent'});
  await expect(single.getByRole('cell', {name: '1 / 2', exact: true})).toBeVisible();
  await expect(single.getByRole('cell', {name: '未核清', exact: true})).toBeVisible();
  const staged = page.getByRole('row').filter({has: page.getByRole('cell', {name: '0.0%（0/2）', exact: true})});
  await expect(staged.getByRole('cell', {name: '1 / 0 / 0 / 1', exact: true})).toBeVisible();
  await expect(page.getByText('预算用尽', {exact: true})).toBeVisible();
  await expect(page.getByText('未运行', {exact: true})).toBeVisible();
  await expect(page.getByRole('cell', {name: '多 Agent', exact: true}).first()).toBeVisible();
  expect(fixtureApi.calls.filter(({path}) => path.startsWith('/api/evaluation/')).every(
    ({path}) => ['/api/evaluation/capabilities', '/api/evaluation/campaigns'].includes(path),
  )).toBe(true);
});

test('official report labels a technical retry and downloads each attempt separately', async ({page, fixtureApi}) => {
  fixtureApi.officialReport = 'combined';
  await signIn(page, 'tester', '/?eval=campaigns');
  await expect(page.getByText('含 1 条 API 异常后的补跑，首次异常记录保留')).toBeVisible();
  await expect(page.getByText('首次待核实 · 第 2 次尝试')).toBeVisible();
  await page.getByText('查看模型、预算和版本', {exact: true}).click();
  await expect(page.getByText('1 次 · 预留 $0.07')).toBeVisible();
  await expect(page.getByText('full / full.repair')).toBeVisible();
  await expect(page.getByRole('columnheader', {name: '官方成功率', exact: true})).toBeVisible();
  await page.getByText('首次运行成绩', {exact: true}).click();
  await expect(page.getByText('强单 Agent，成功 1/2，未评分 1')).toBeVisible();
  for (const [kind, button, score] of [
    ['primary', '下载首次运行报告', null],
    ['repair', '下载补跑报告', 0],
  ]) {
    const pending = page.waitForEvent('download');
    await page.getByRole('button', {name: button}).click();
    const download = await pending;
    expect(download.suggestedFilename()).toBe(`tau-official-${kind}.json`);
    const report = JSON.parse(await readFile(await download.path(), 'utf8'));
    expect(report.rows.find(row => row.architecture === 'strong_single' && row.task_id === 'demo-b').official_reward).toBe(score);
  }
  expect(fixtureApi.calls.filter(({path}) => path.includes('/campaigns/tau-official-')).map(({path, role}) => [path, role]))
    .toEqual([['/api/evaluation/campaigns/tau-official-primary', 'tester'], ['/api/evaluation/campaigns/tau-official-repair', 'tester']]);
});

test('batch selection keeps measured versions, coverage and original downloads in their own batch', async ({page, fixtureApi}) => {
  const group = {architecture: 'M2', scheduled: 2, passed: 1, attempts: 3, traces: 2, partial: 1,
    errors: 1, tools: 4, unknown_tools: 0, steps_n: 1, avg_steps: 3, latency_n: 2, p95_seconds: 5,
    calls: 3, priced: 2, metered: 2, ledger_gaps: 0, input_tokens: 100, output_tokens: 20,
    cached_input_tokens: 40, cost_per_success: null};
  const first = {id: 'e3', label: '训练集实验', note: '冻结训练题', revision: '001122334455',
    split: 'train', tasks: 2, trials: 1, commit: 'aabbccddeeff', kernel: 'v3.1', patched: false,
    adapter: null, repairs: 1, groups: [group]};
  fixtureApi.officialReport = 'combined';
  fixtureApi.benchmarkBatches = [first, {...first, id: 'original', label: '首轮对照', commit: null, split: 'test'}];
  await signIn(page, 'tester', '/?eval=campaigns');
  const metrics = page.getByRole('region', {name: '评测批次指标'});
  await expect(metrics.getByText('v3.1 · aabbccddeeff')).toBeVisible();
  await expect(metrics.getByText('25.0%')).toBeVisible();
  await expect(metrics.getByRole('cell', {name: /^120 输入 100 · 输出 20 其中缓存输入 40/})).toBeVisible();
  await expect(metrics.getByText('覆盖 1/2 题')).toBeVisible();
  await expect(metrics.getByText('费用未核清')).toBeVisible();
  await expect(metrics.getByText('5.00 秒')).toBeVisible();
  await expect(page.getByRole('button', {name: '下载首次运行报告'})).toHaveCount(0);
  await page.getByRole('combobox', {name: '选择评测批次'}).click();
  await page.getByText('首轮对照', {exact: true}).click();
  await expect(metrics.getByText(/提交未记录/)).toBeVisible();
  await expect(page.getByRole('button', {name: '下载首次运行报告'})).toBeVisible();
});
