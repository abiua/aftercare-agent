import assert from 'node:assert/strict';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const output = 'reports/plain-language-original-display-20261112';
await mkdir(output, {recursive:true});
const browser = await chromium.launch({headless:true, executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const checks=[];
try {
 const page = await browser.newPage({viewport:{width:1440,height:1100}});
 await page.route('**/api/**', route => {
  const r=route.request();
  return ['GET','HEAD'].includes(r.method()) || new URL(r.url()).pathname === '/api/auth/demo-role' ? route.continue() : route.abort();
 });
 for (const [order,id] of [['O9962','dab456a8-8fdd-44f6-bf2d-86923d1eff93'],['O9961','e019964c-5402-4e1b-b68f-49188686af5b']]) {
  await page.goto(`http://127.0.0.1:5225/?case=${id}`);
  const login=page.getByRole('button',{name:'以客服身份进入',exact:true});
  if(order === 'O9962') await login.click();
  await page.getByRole('tab',{name:'Agent 建议与客服审核',exact:true}).click();
  await page.getByText('这些商品需要分开处理，请客服调整方案',{exact:true}).first().waitFor();
  await page.getByRole('heading', {name:/助手建议 · 第/}).waitFor();
  const text=await page.locator('body').innerText();
  const source = order === 'O9961' ? 'tau-p5-continuation-browser-o9961-structured-recheck-20261112' : 'tau-p5-continuation-browser-o9962-final-verifier-20261112';
  const original = JSON.parse(await readFile(`reports/${source}/report.json`));
  for (const option of original.decision.options) assert.ok(text.includes(option.rationale), 'Generated prose must be shown unchanged');
  await page.screenshot({path:`${output}/${order}.png`,fullPage:true});
  await writeFile(`${output}/${order}.txt`,text);
  checks.push({order,generated_prose_unchanged:true,blocked_reason_visible:true});
 }
 await writeFile(`${output}/report.json`,JSON.stringify({checks,business_mutations:0,model_calls:0},null,2));
} finally {await browser.close();}
