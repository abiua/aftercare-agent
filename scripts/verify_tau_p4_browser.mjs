import assert from 'node:assert/strict';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';

const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const root = new URL('../', import.meta.url);
const users = JSON.parse(await readFile(new URL('.runtime/tau-p4-demo/.runtime/users-test.json', root)));
const prior = JSON.parse(await readFile(new URL('reports/tau-p4-chat-http-20261112.json', root)));
const target = new URL('reports/tau-p4-browser-20261112/', root);
await mkdir(target, {recursive: true});
const report = {status: 'RUNNING', stage: 'launch', conversation_id: prior.conversation_id};
const browser = await chromium.launch({headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const contexts = [];
async function detail(role) {
  const response = await fetch(`http://127.0.0.1:18224/api/conversations/${prior.conversation_id}`, {
    headers: {Authorization: `Bearer ${users.find((row) => row.role === role).token}`},
  });
  assert.equal(response.status, 200);
  return response.json();
}
async function login(role, id) {
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  contexts.push(context);
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:5224/?conversation=${id}`);
  report.stage = `login_form_${role}`;
  const loginLabel = ({customer:'客户',support:'客服',approver:'审批人',tester:'测试员'})[role];
  await page.getByRole('button', {name: `以${loginLabel}身份进入`, exact: true}).click();
  report.stage = `chat_loaded_${role}`;
  await page.getByLabel(role === 'customer' ? '描述售后问题' : '公开回复', {exact: true}).waitFor();
  return page;
}
try {
  report.stage = 'policy_customer_ui';
  const customer = await login('customer', prior.policy_conversation);
  report.stage = 'policy_citation';
  await customer.getByText('查看政策依据', {exact: true}).click();
  report.stage = 'policy_title';
  await customer.getByText('签收后退货退款', {exact: true}).waitFor();
  assert.ok(await customer.getByText('普通退货获准后安排寄回。收到退件并验货通过后才可提交退款审核。', {exact: true}).isVisible());
  await customer.screenshot({path: new URL('policy.png', target).pathname, fullPage: true});
  await customer.reload();
  await customer.getByText('查看政策依据', {exact: true}).waitFor();
  report.policy_reload = true;

  report.stage = 'customer_correction';
  const before = await detail('customer');
  report.before = {case_id: before.conversation.active_case_id, issue_id: before.memory.issue_id};
  await customer.goto(`http://127.0.0.1:5224/?conversation=${prior.conversation_id}`);
  const correction = '更正一下，裂纹是在杯底，我仍申请处理这一件商品。';
  const resumed = before.memory?.state === 'CURRENT' && before.memory?.latest_request?.text === correction;
  report.resumed_after_committed_confirmation = resumed;
  if (!resumed && (await detail('customer')).memory?.correction?.text !== correction) {
  await customer.getByLabel('描述售后问题').fill(correction);
  await customer.getByRole('button', {name: '发送消息', exact: true}).click();
  }
  await customer.getByText(correction, {exact: true}).waitFor();
  if (!resumed) {
  let pending;
  for (let i = 0; i < 20; i++) {
    pending = await detail('customer');
    if (pending.memory?.state === 'CORRECTION_NEEDS_SUPPORT') break;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  assert.equal(pending.memory.state, 'CORRECTION_NEEDS_SUPPORT');
  assert.equal(pending.conversation.active_case_id, report.before.case_id);

  }
  report.stage = 'support_confirmation_ui';
  const support = await login('support', prior.conversation_id);
  if (!resumed) {
  await support.getByText('确认客户更正', {exact: true}).waitFor();
  await support.getByRole('checkbox', {name: 'before.png', exact: true}).check();
  await support.getByLabel('照片复用理由').fill('保留当前事项原图供重新核查，不沿用旧图片结论。');
  await support.getByRole('button', {name: '确认更正并继续核查', exact: true}).scrollIntoViewIfNeeded();
  await support.screenshot({path: new URL('correction-review.png', target).pathname, fullPage: true});
  const committed = support.waitForResponse((response) => response.url().endsWith('/correction/confirm') && response.request().method() === 'POST');
  await support.getByRole('button', {name: '确认更正并继续核查', exact: true}).click();
  assert.equal((await committed).status(), 200);
  await support.getByRole('button', {name: '确认更正并继续核查', exact: true}).waitFor({state: 'hidden'});
  }
  const after = await detail('support');
  assert.equal(after.conversation.active_case_id, report.before.case_id);
  assert.equal(after.memory.issue_id, report.before.issue_id);
  assert.equal(after.memory.state, 'CURRENT');
  assert.equal(after.memory.latest_request.text, correction);
  assert.equal(after.memory.confirmed_correction.retained_attachments.length, 1);
  await support.reload();
  await support.getByLabel('公开回复', {exact: true}).waitFor();
  assert.equal(await support.getByRole('button', {name: '确认更正并继续核查', exact: true}).count(), 0);
  await customer.reload();
  await customer.getByText(correction, {exact: true}).waitFor();
  assert.equal(await customer.getByLabel('照片复用理由').count(), 0);
  await customer.screenshot({path: new URL('correction-customer.png', target).pathname, fullPage: true});
  report.after = {case_id: after.conversation.active_case_id, memory: after.memory};
  report.support_refresh = true;
  report.customer_has_no_support_controls = true;
  report.stage = 'complete';
  report.status = 'PASSED';
} catch (error) {
  report.status = 'FAILED';
  report.error_type = error.name;
  report.error_detail = String(error.message).split('Call log:')[0].slice(0,1000);
  if (process.env.AFTERCARE_DEMO_PASSWORD) report.error_detail = report.error_detail.replaceAll(process.env.AFTERCARE_DEMO_PASSWORD, '[REDACTED]');
  for (const user of users) report.error_detail = report.error_detail.replaceAll(user.token, '[REDACTED]');
  process.exitCode = 1;
} finally {
  for (const context of contexts) await context.close();
  await browser.close();
  await writeFile(new URL('report.json', target), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({status: report.status, stage: report.stage}));
}
