import { expect } from '@playwright/test';
import { progressExperimentId, signIn, test } from './api-fixture.mjs';

async function openProgress(page) {
  await signIn(page, 'tester');
  await page.goto(`/?eval=experiments&eval_experiment=${progressExperimentId}`);
  return page.getByRole('dialog', {name: '受控进度实验'});
}

test('fixture: reconnects from the last accepted cursor and closes only after cancelled work settles', async ({page, fixtureApi}) => {
  fixtureApi.progressMode = 'stream';
  const dialog = await openProgress(page);
  try {
    await expect(dialog.getByText('已取消', {exact: true}).first()).toBeVisible();
    await expect(dialog.getByText('进度已结束', {exact: true})).toHaveCount(0);
    await expect.poll(() => fixtureApi.progressStreams).toEqual([{after: '0', lastEventId: undefined}, {after: '1', lastEventId: '1'}]);
  } finally {
    fixtureApi.releaseProgress();
  }
  await expect(dialog.getByText('进度已结束', {exact: true})).toBeVisible();
  await expect(dialog.getByText('失败', {exact: true}).last()).toBeVisible();
  expect(fixtureApi.progressStreams).toHaveLength(2);
  expect(fixtureApi.progressProbes).toEqual([{after: '1', limit: '1'}]);
  expect(fixtureApi.calls.every(({method}) => method === 'GET')).toBe(true);
});

test('fixture: an active historical snapshot switches to polling without reconnecting', async ({page, fixtureApi}) => {
  fixtureApi.progressMode = 'historical';
  const dialog = await openProgress(page);
  await expect(dialog.getByText('定时更新', {exact: true})).toBeVisible();
  await expect(dialog.getByText('已完成', {exact: true}).first()).toBeVisible();
  await expect(dialog.getByText('定时更新', {exact: true})).toHaveCount(0);
  expect(fixtureApi.progressStreams).toHaveLength(1);
  expect(fixtureApi.progressReads).toBeGreaterThanOrEqual(3);
});

test('fixture: revoked stream access stops reconnecting and never switches to polling', async ({page, fixtureApi}) => {
  fixtureApi.progressMode = 'revoked';
  const dialog = await openProgress(page);
  await expect(dialog.getByText('当前身份无法读取进度', {exact: true})).toBeVisible();
  await expect(dialog.getByText('定时更新', {exact: true})).toHaveCount(0);
  expect(fixtureApi.progressStreams).toEqual([{after: '0', lastEventId: undefined}]);
  expect(fixtureApi.progressProbes).toEqual([{after: '1', limit: '1'}]);
});

test('fixture: a missing progress page switches to snapshots without a second stream', async ({page, fixtureApi}) => {
  fixtureApi.progressMode = 'gap';
  const dialog = await openProgress(page);
  await expect.poll(() => fixtureApi.progressProbes).toEqual([{after: '1', limit: '1'}]);
  await expect(dialog.getByText('定时更新', {exact: true})).toBeVisible();
  expect(fixtureApi.progressStreams).toEqual([{after: '0', lastEventId: undefined}]);
});

test('fixture: repeated disconnects have a bounded retry budget before fallback', async ({page, fixtureApi}) => {
  fixtureApi.progressMode = 'network';
  const dialog = await openProgress(page);
  await expect.poll(() => fixtureApi.progressStreams.length, {timeout: 12_000}).toBe(5);
  await expect(dialog.getByText('定时更新', {exact: true})).toBeVisible();
  expect(fixtureApi.progressStreams).toHaveLength(5);
  expect(fixtureApi.progressProbes).toHaveLength(4);
});
