import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require = createRequire(new URL('../frontend/tests/browser/package.json', import.meta.url));
const {chromium} = require('@playwright/test');
const browser = await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const report = {status:'RUNNING',roles:[]};
try {
  for (const [role,label] of [['customer','客户'],['support','客服'],['approver','财务审批人'],['tester','测试员']]) {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const page = await context.newPage();
    await page.goto('http://127.0.0.1:5224/');
    await page.getByRole('button',{name:`以${label}身份进入`,exact:true}).waitFor();
    assert.equal(await page.locator('input').count(),0);
    if (role === 'customer') await page.screenshot({path:'reports/demo-role-login.png',fullPage:true});
    const meResponse = page.waitForResponse(r=>r.url().endsWith('/api/me') && r.status()===200);
    await page.getByRole('button',{name:`以${label}身份进入`,exact:true}).click();
    assert.equal((await (await meResponse).json()).role,role);
    await page.getByRole('button',{name:/退\s*出/}).waitFor();
    await page.reload();
    await page.getByRole('button',{name:/退\s*出/}).click();
    await page.getByRole('button',{name:'以客户身份进入',exact:true}).waitFor();
    report.roles.push({role,login:true,refresh:true,logout:true});
    await context.close();
  }
  const invalid = await fetch('http://127.0.0.1:18224/api/auth/demo-role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:'admin'})});
  assert.equal(invalid.status,422);
  report.invalid_role = 'REJECTED';
  report.status = 'PASSED';
} catch(error) {
  report.status='FAILED'; report.error_type=error.name; throw error;
} finally {
  await browser.close();
  await writeFile('reports/demo-role-login.json',JSON.stringify(report,null,2)+'\n');
}
console.log(JSON.stringify(report));
