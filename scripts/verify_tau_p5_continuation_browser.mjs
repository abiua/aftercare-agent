import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {join, resolve} from 'node:path';

const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const [livePath, outputDir] = process.argv.slice(2);
assert.ok(livePath && outputDir, 'Usage: node scripts/verify_tau_p5_continuation_browser.mjs LIVE_REPORT NEW_OUTPUT_DIR');
const live = JSON.parse(await readFile(livePath));
const orderId = live.order.id;
assert.ok(['O9961', 'O9962'].includes(orderId));
assert.equal(live.stage, 'live_readback');
const usage = live.paid_usage;
const runId = usage.run.id;
const target = resolve(outputDir);
const report = {
  status: 'RUNNING', order_id: orderId, case_id: live.case_id,
  conversation_id: live.conversation_id, run_id: runId, blocked_mutations: [],
};
const url = 'http://127.0.0.1:5225';
const decisionsPath = `/api/conversations/${live.conversation_id}/decisions`;
const eventsPath = `/api/cases/${live.case_id}/runs/${runId}/events`;
const matches = (path) => (response) => new URL(response.url()).pathname === path;
const manifest = JSON.parse(await readFile(new URL('../data/vision-v1/manifest.json', import.meta.url)));
await mkdir(target);

let browser;
try {
  browser = await chromium.launch({
    headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, serviceWorkers: 'block'});
  await context.route('**/api/**', (route) => {
    const request = route.request();
    const method = request.method();
    const {pathname} = new URL(request.url());
    if (['GET', 'HEAD'].includes(method) || (method === 'POST' && pathname === '/api/auth/demo-role')) {
      return route.continue();
    }
    report.blocked_mutations.push({method, pathname});
    return route.abort('blockedbyclient');
  });

  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  report.stage = 'decision';
  await page.goto(`${url}/?case=${live.case_id}`);
  const decisionRead = page.waitForResponse(matches(decisionsPath));
  await page.getByRole('button', {name: '以客服身份进入', exact: true}).click();
  await page.getByRole('tab', {name: 'Agent 建议与客服审核', exact: true}).click();
  const decision = await decisionRead;
  assert.equal(decision.status(), 200);
  const {snapshot} = await decision.json();
  assert.ok(snapshot, 'Decision snapshot missing');
  assert.equal(snapshot.id, usage.projected_snapshot?.id);
  assert.equal(snapshot.sha256, usage.projected_snapshot?.sha256);
  assert.deepEqual(snapshot.checks, usage.projected_snapshot?.checks);
  assert.equal(snapshot.input.order_id, orderId);
  await page.getByRole('heading', {name: `助手建议 · 第 ${snapshot.version} 版`, exact: true}).waitFor();
  report.decision = {
    snapshot_id: snapshot.id, version: snapshot.version, sha256: snapshot.sha256,
    options: snapshot.candidate.options, unresolved: snapshot.candidate.unresolved, checks: snapshot.checks,
  };
  await page.locator('#case-decision-review').scrollIntoViewIfNeeded();
  await page.screenshot({path: join(target, 'decision.png'), fullPage: true});
  if (live.revises?.snapshot_id && live.revises?.snapshot_sha256) {
    await page.getByRole('button', {name: '查看历史方案', exact: true}).click();
    const earlierRead = page.waitForResponse(matches(`${decisionsPath}/history/${live.revises.snapshot_id}`));
    await page.getByRole('button', {name: `第 ${snapshot.version - 1} 版 · 助手建议`, exact: true}).click();
    const earlierResponse = await earlierRead;
    assert.equal(earlierResponse.status(), 200);
    const earlier = await earlierResponse.json();
    assert.equal(earlier.id, live.revises.snapshot_id);
    assert.equal(earlier.sha256, live.revises.snapshot_sha256);
    await page.getByText('历史记录仅供查看。', {exact: true}).waitFor();
    report.previous_decision = {id: earlier.id, version: earlier.version, sha256: earlier.sha256};
    await page.getByRole('button', {name: '返回当前方案', exact: true}).click();
  }

  report.stage = 'investigation';
  const eventRead = page.waitForResponse(matches(eventsPath));
  await page.getByRole('tab', {name: '调查过程与运行记录', exact: true}).click();
  const history = await eventRead;
  assert.equal(history.status(), 200);
  const events = await history.json();
  for (const [role, label] of [['fact', '事实核对'], ['policy', '政策核对']]) {
    assert.ok(events.some((event) => event.kind === 'investigation_input' && event.payload.role === role));
    await page.getByRole('heading', {name: `${label} · 开始核对`, exact: true}).first().waitFor();
  }
  const comparedKinds = [
    'investigation_recheck', 'investigation_claim_revision', 'investigation_assessment', 'investigation_result',
  ];
  for (const kind of comparedKinds) {
    const payloads = (rows) => rows.filter((row) => row.kind === kind).map((row) => row.payload);
    assert.deepEqual(payloads(events), payloads(usage.events), kind);
  }
  const recheck = events.find(({kind}) => kind === 'investigation_recheck')?.payload;
  const revision = events.find(({kind}) => kind === 'investigation_claim_revision')?.payload;
  if (recheck) {
    await page.getByRole('heading', {name: /局部复查/}).first().waitFor();
    if (recheck.executor_role === 'fact' && recheck.target_role === 'policy') {
      await page.getByText('记录查询：事实核对 · 判断修订：政策核对', {exact: true}).waitFor();
    }
  }
  if (revision) {
    const heading = page.getByRole('heading', {name: '局部补查后的判断变更', exact: true});
    await heading.waitFor();
    for (const claim of revision.retired_claims) {
      await page.getByText(`原判断（已撤销）：${claim.value || '未记录'}`, {exact: true}).first().waitFor();
    }
    for (const claim of revision.replacement_claims) {
      await page.getByText(`补查后：${claim.value} ·`, {exact: false}).first().waitFor();
    }
    await heading.scrollIntoViewIfNeeded();
    await page.screenshot({path: join(target, 'revision.png')});
  }
  report.investigation = {
    event_count: events.length,
    compared_event_kinds: comparedKinds,
    recheck_events: events.filter(({kind}) => kind === 'investigation_recheck').length,
    revision_events: events.filter(({kind}) => kind === 'investigation_claim_revision').length,
    executor_role: recheck?.executor_role,
    target_role: recheck?.target_role,
    model_timeout: usage.attempts.some((row) => row.run_id === runId && row.error_code === 'MODEL_TIMEOUT') ||
      JSON.stringify(events).includes('MODEL_TIMEOUT'),
  };
  await page.screenshot({path: join(target, 'investigation.png'), fullPage: true});

  report.stage = 'originals';
  await page.goto(`${url}/?conversation=${live.conversation_id}`);
  report.originals = [];
  for (const fixture of live.fixture.attachments) {
    const asset = manifest.assets.find(({id}) => id === fixture.asset_id);
    const material = live.materials.find(({kind}) => kind === fixture.kind);
    assert.equal(material.sha256, asset.sha256);
    const file = asset.file.split('/').at(-1);
    const attachment = `/api/conversations/${live.conversation_id}/attachments/${material.attachment_id}/content`;
    const imageRead = page.waitForResponse(matches(attachment));
    await page.locator('.chat-photos').getByRole('button', {name: `查看照片 · ${file}`, exact: true}).first().click();
    const response = await imageRead;
    assert.equal(response.status(), 200);
    const dialog = page.getByRole('dialog', {name: `照片预览 · ${file}`});
    const image = dialog.getByRole('img', {name: `原图 · ${file}`, exact: true});
    await image.waitFor();
    const {dimensions, bytes} = await image.evaluate(async (element) => {
      await element.decode();
      const blob = await fetch(element.src);
      return {
        dimensions: [element.naturalWidth, element.naturalHeight],
        bytes: Array.from(new Uint8Array(await blob.arrayBuffer())),
      };
    });
    const sha256 = createHash('sha256').update(Buffer.from(bytes)).digest('hex');
    assert.equal(sha256, asset.sha256);
    assert.ok(dimensions.every((size) => size > 0));
    report.originals.push({asset_id: asset.id, attachment_id: material.attachment_id, sha256, dimensions});
    await page.screenshot({path: join(target, `original-${asset.id}.png`)});
    await dialog.getByRole('button', {name: '关闭预览', exact: true}).click();
  }

  report.stage = 'candidate_readback';
  assert.deepEqual(report.blocked_mutations, []);
  const options = snapshot.candidate.options;
  const l01Refunds = options.filter(({action}) => action === 'REFUND')
    .flatMap(({lines}) => lines).filter(({line_id}) => line_id === 'L01');
  const targetedL02 = options.some((option) =>
    ['REQUEST_EVIDENCE', 'HUMAN_HANDOFF'].includes(option.action) &&
    JSON.stringify(option.target_line_ids) === '["L02"]');
  report.candidate_observations = {
    l01_refund_amounts_cents: l01Refunds.map(({amount_cents}) => amount_cents),
    targeted_l02_followup: targetedL02,
    synthesized_handoff_only: options.length === 1 && options[0].id === 'graph-disposition',
  };
  if (orderId === 'O9961') {
    assert.ok(l01Refunds.every(({amount_cents}) => amount_cents <= 3000), 'L01 refund exceeds ¥30.00');
  }
  report.status = 'PASSED';
  report.stage = 'complete';
} catch (error) {
  report.status = 'FAILED';
  report.error = String(error.message).split('Call log:')[0].slice(0, 1000);
  process.exitCode = 1;
} finally {
  await browser?.close();
  await writeFile(join(target, 'report.json'), JSON.stringify(report, null, 2) + '\n', {flag: 'wx'});
  console.log(JSON.stringify(report));
}
