import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const password = process.env.AFTERCARE_DEMO_PASSWORD;
if (!password) throw new Error('Set AFTERCARE_DEMO_PASSWORD');
const report = {status: 'RUNNING', roles: []};
for (const [username, role] of [['customer', 'customer'], ['support', 'support'], ['finance', 'approver'], ['tester', 'tester']]) {
  const result = await fetch('http://127.0.0.1:18224/api/auth/login', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({username,password})});
  assert.equal(result.status, 200);
  assert.equal(result.headers.get('cache-control'), 'no-store');
  const {token} = await result.json();
  const me = await fetch('http://127.0.0.1:18224/api/me', {headers: {Authorization:`Bearer ${token}`}});
  assert.equal((await me.json()).role, role);
  report.roles.push({username, role, status:'PASSED'});
}
const bad = await fetch('http://127.0.0.1:18224/api/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'customer',password:'invalid-password'})});
assert.equal(bad.status,401); report.wrong_password = 'REJECTED';
const browser = await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
try {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  await page.goto('http://127.0.0.1:5224/');
  assert.equal(await page.getByLabel('访问凭据',{exact:true}).count(),0);
  await page.getByLabel('账号',{exact:true}).fill('customer');
  await page.getByLabel('密码',{exact:true}).fill('invalid-password');
  await page.getByRole('button',{name:/登\s*录/}).click();
  await page.getByText('账号或密码不正确。',{exact:true}).waitFor();
  await page.getByLabel('密码',{exact:true}).fill(password);
  await page.getByRole('button',{name:/登\s*录/}).click();
  await page.getByRole('button',{name:/退\s*出/}).waitFor();
  await page.getByRole('button',{name:/订单 O9902/}).waitFor();
  await page.screenshot({path:'reports/demo-password-login.png',fullPage:true});
  await page.reload();
  await page.getByRole('button',{name:/退\s*出/}).click();
  await page.getByLabel('账号',{exact:true}).waitFor();
  report.browser = {password_login:true,old_cases_visible:true,refresh:true,logout:true};
  report.status = 'PASSED';
} catch (error) {
  report.status = 'FAILED';
  report.error_type = error.name;
  throw error;
} finally {
  await browser.close();
  await writeFile('reports/demo-password-login.json',JSON.stringify(report,null,2)+'\n');
}
console.log(JSON.stringify(report));
