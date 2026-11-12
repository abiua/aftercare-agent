import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const root = new URL('../', import.meta.url);
const users = JSON.parse(await readFile(new URL('.runtime/tau-p4-demo/.runtime/users-test.json', root)));
const prior = JSON.parse(await readFile(new URL('reports/tau-p4-chat-http-20261112.json', root)));
const business = JSON.parse(await readFile(new URL('reports/tau-p4-live-business-final-20261112.json', root)));
const target = new URL('reports/tau-p4-browser-20261112/', root);
const token = users.find((user) => user.role === 'customer').token;
const browser = await chromium.launch({headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
const report = {status: 'RUNNING', stage: 'login'};
try {
  await page.goto(`http://127.0.0.1:5224/?conversation=${prior.policy_conversation}`);
  const loginLabel = '客户';
  await page.getByRole('button', {name: `以${loginLabel}身份进入`, exact: true}).click();
  const question = '请问退货政策的具体规则是什么？';
  await page.getByLabel('描述售后问题', {exact: true}).waitFor();
  report.stage = 'policy_direct_answer';
  // Re-running resumes an already committed question instead of sending duplicates.
  if (!await page.getByText(question, {exact: true}).count()) {
    await page.getByLabel('描述售后问题', {exact: true}).fill(question);
    const posted = page.waitForResponse((response) => response.url().includes('/messages') && response.request().method() === 'POST');
    await page.getByRole('button', {name: '发送消息', exact: true}).click();
    assert.equal((await posted).status(), 201);
  }
  const answer = page.getByText('支持普通退货的商品可在签收后168小时内提出申请，含恰好168小时的时刻。普通退货获准后安排寄回。收到退件并验货通过后才可提交退款审核。需要核对具体商品时，请先确认订单。', {exact: true});
  await answer.waitFor({timeout: 60000});
  await page.screenshot({path: new URL('policy-direct.png', target).pathname, fullPage: true});
  report.policy_direct_answer = true;
  report.stage = 'customer_execution_receipt';
  await page.goto(`http://127.0.0.1:5224/?conversation=${business.conversation_id}`);
  await page.getByText('本次售后执行进度', {exact: true}).waitFor();
  await page.getByText('模拟退款 · 执行成功', {exact: true}).waitFor();
  assert.ok((await page.locator('body').innerText()).includes('49.00'));
  assert.equal(await page.locator('.chat-analysis-status').count(), 0);
  await page.screenshot({path: new URL('refund-success.png', target).pathname, fullPage: true});
  await page.reload();
  await page.getByText('模拟退款 · 执行成功', {exact: true}).waitFor();
  report.receipt_refresh = true;
  report.conversation_id = business.conversation_id;
  report.status = 'PASSED'; report.stage = 'complete';
} catch (error) {
  report.status = 'FAILED'; report.error_type = error.name;
  report.error_detail = String(error.message).split('Call log:')[0].slice(0, 1000).replaceAll(token, '[REDACTED]');
  if (process.env.AFTERCARE_DEMO_PASSWORD) report.error_detail = report.error_detail.replaceAll(process.env.AFTERCARE_DEMO_PASSWORD, '[REDACTED]');
  process.exitCode = 1;
} finally {
  await browser.close();
  await writeFile(new URL('final-ui.json', target), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
}
