import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';

const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const root = new URL('../', import.meta.url);
const target = new URL('reports/tau-p5-browser-20261112/', root);
const read = async (name) => JSON.parse(await readFile(new URL(name, root)));
const live = await read('reports/tau-p5-live-o9951-20261112.json');
const manifest = await read('data/vision-v1/manifest.json');
const url = 'http://127.0.0.1:5225';
const casePath = `/?case=${live.case_id}`;
const eventPath = new RegExp(`/api/cases/${live.case_id}/runs/[^/]+/events\\?after=0$`);
const report = {status: 'RUNNING', case_id: live.case_id, conversation_id: live.conversation_id};
const browser = await chromium.launch({headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
await mkdir(target, {recursive: true});

async function session() {
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.route('**/api/**', (route) => {
    const request = route.request();
    if (['GET', 'HEAD'].includes(request.method()) ||
        (request.method() === 'POST' && new URL(request.url()).pathname === '/api/auth/demo-role')) {
      return route.continue();
    }
    return route.abort('blockedbyclient');
  });
  const page = await context.newPage();
  await page.goto(url + casePath);
  await page.getByRole('button', {name: '以客服身份进入', exact: true}).click();
  return {context, page};
}

async function openTrace(page) {
  await page.getByRole('tab', {name: '调查过程与运行记录', exact: true}).click();
}

try {
  report.stage = 'real_events';
  const real = await session();
  const history = real.page.waitForResponse((response) => eventPath.test(response.url()));
  await openTrace(real.page);
  const response = await history;
  assert.equal(response.status(), 200);
  const events = await response.json();
  for (const [role, label] of [['fact', '事实核对'], ['policy', '政策核对']]) {
    assert.ok(events.some((event) => event.kind === 'investigation_input' && event.payload.role === role));
    await real.page.getByRole('heading', {name: `${label} · 开始核对`, exact: true}).first().waitFor();
  }
  report.real_events = {
    run_id: new URL(response.url()).pathname.split('/').at(-2),
    roles: ['fact', 'policy'],
    recheck_events: events.filter((event) => event.kind === 'investigation_recheck').length,
    revision_events: events.filter((event) => event.kind === 'investigation_claim_revision').length,
  };
  await real.page.screenshot({path: new URL('real-investigation.png', target).pathname, fullPage: true});

  report.stage = 'real_originals';
  await real.page.goto(`${url}/?conversation=${live.conversation_id}`);
  report.real_originals = [];
  for (const assetId of ['VIS-01', 'VIS-03']) {
    const asset = manifest.assets.find((item) => item.id === assetId);
    const file = asset.file.split('/').at(-1);
    const photo = real.page.waitForResponse((item) => /\/attachments\/[^/]+\/content$/.test(new URL(item.url()).pathname));
    await real.page.locator('.chat-photos').getByRole('button', {name: `查看照片 · ${file}`, exact: true}).first().click();
    const received = await photo;
    assert.equal(received.status(), 200);
    const dialog = real.page.getByRole('dialog', {name: `照片预览 · ${file}`});
    const image = dialog.getByRole('img', {name: `原图 · ${file}`, exact: true});
    await image.waitFor();
    const bytes = await image.evaluate(async (element) => {
      await element.decode();
      const blob = await fetch(element.src);
      return Array.from(new Uint8Array(await blob.arrayBuffer()));
    });
    const hash = createHash('sha256').update(Buffer.from(bytes)).digest('hex');
    assert.equal(hash, asset.sha256);
    const dimensions = await image.evaluate((element) => [element.naturalWidth, element.naturalHeight]);
    assert.ok(dimensions.every((size) => size > 0));
    report.real_originals.push({asset_id: assetId, sha256: hash, dimensions});
    await real.page.screenshot({path: new URL(`real-original-${assetId}.png`, target).pathname});
    await dialog.getByRole('button', {name: '关闭预览', exact: true}).click();
  }
  await real.context.close();

  report.stage = 'browser_only_fixture';
  const simulated = await session();
  let interceptions = 0;
  await simulated.context.route(eventPath, async (route) => {
    const original = await route.fetch();
    assert.equal(original.status(), 200);
    const events = await original.json();
    const seq = Math.max(0, ...events.map((event) => event.seq));
    const claim = {id: 'fact/browser-balance', role: 'fact', subject: 'L01 浏览器测试余额',
      predicate: 'remaining', value: '可退49元', state: 'SUPPORTED', supporting_refs: ['L01']};
    const event = (index, suffix, payload) => ({seq: seq + index, kind: `investigation_${suffix}`,
      payload, created_at: new Date().toISOString()});
    await route.fulfill({response: original, json: [...events,
      event(1, 'recheck', {target_role: 'fact', claim_ids: [claim.id], stale_claim_ids: [claim.id],
        tool: 'read_order_lines', evidence_ids: ['L01'], reason: '浏览器测试：核对历史累计退款',
        counterexamples: [{claim_id: claim.id, contradicted_field: 'remaining', verdict: 'CONTRADICTED', evidence_refs: ['L01']}]}),
      event(2, 'claim_revision', {target_role: 'fact', target_claim_ids: [claim.id],
        retired_claims: [claim], replacement_claims: [{...claim, value: '可退30元'}],
        unresolved_stale_claim_ids: []}),
    ]});
    interceptions += 1;
  });
  await openTrace(simulated.page);
  await simulated.page.getByRole('heading', {name: '局部补查后的判断变更', exact: true}).waitFor();
  await simulated.page.getByText('原判断（已撤销）：可退49元', {exact: true}).waitFor();
  await simulated.page.getByText('补查后：可退30元 · 有支持证据', {exact: true}).waitFor();
  assert.ok(interceptions >= 1);
  await simulated.page.getByRole('heading', {name: '局部补查后的判断变更', exact: true}).scrollIntoViewIfNeeded();
  await simulated.page.screenshot({path: new URL('browser-fixture-revision.png', target).pathname});
  report.browser_only_fixture = {interceptions, retired: '可退49元', replacement: '可退30元', real_model_evidence: false};
  await simulated.context.close();
  report.status = 'PASSED';
  report.stage = 'complete';
} catch (error) {
  report.status = 'FAILED';
  report.error = String(error.message).split('Call log:')[0].slice(0, 1000);
  process.exitCode = 1;
} finally {
  await browser.close();
  await writeFile(new URL('report.json', target), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
}
