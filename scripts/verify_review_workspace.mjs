import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir,writeFile} from 'node:fs/promises';
const require=createRequire(new URL('../frontend/tests/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const caseId='d4b92f75-f54e-4872-99a5-01b5c908b01c';
const dir='reports/review-workspace-20261112';await mkdir(dir,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const report={status:'RUNNING',checks:[]};
try {
 for(const [role,label,tab] of [['support','客服','Agent 建议与客服审核'],['approver','财务审批人','财务审批与依据']]) {
  const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();
  await page.goto(`http://127.0.0.1:5224/?case=${caseId}`);
  await page.getByRole('button',{name:`以${label}身份进入`,exact:true}).click();
  await page.getByRole('tab',{name:tab,selected:true}).waitFor();
  await page.getByText('Agent 的意见与决策依据',{exact:true}).waitFor();
  await page.getByText('损坏处置',{exact:false}).first().waitFor();
  assert.ok((await page.locator('body').innerText()).includes('49.00'));
  assert.equal(await page.getByRole('button',{name:'审核财务方案',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'采纳',exact:true}).count(),0);
  const heading=await page.getByText(role==='support'?'Agent 的意见与决策依据':'财务审批 · 当前处理方案',{exact:true}).boundingBox();
  assert.ok(heading.y<700);
  await page.screenshot({path:`${dir}/${role}.png`,fullPage:true});
  await page.reload();await page.getByRole('tab',{name:tab,selected:true}).waitFor();
  report.checks.push({role,default_tab:tab,actual_agent_amount_visible:true,completed_case_no_repeat_actions:true,first_card_y:heading.y});
  // Browser-only presentation fixture. No business state or approval is submitted.
  await page.route(`**/api/cases/${caseId}`, async route => {
    const response=await route.fetch(); const body=await response.json();
    body.case.status=role==='approver'?'PROPOSED':'HUMAN_REVIEW';
    body.ledger=[];body.approvals=[];
    body.proposals=body.proposals.map((p,i,all)=>({...p,status:i===all.length-1?'PROPOSED':p.status}));
    await route.fulfill({response,json:body});
  });
  await page.reload();
  if(role==='approver') {
    const action=page.getByRole('button',{name:'审核财务方案',exact:true});
    await action.waitFor();assert.ok((await action.boundingBox()).y<700);await action.click();
    await page.getByRole('dialog').getByText('审批结论',{exact:true}).waitFor();
  } else {
    await page.getByRole('button',{name:'审核 / 修改 Agent 方案',exact:true}).click();
    await page.getByRole('button',{name:'修改',exact:true}).click();
    await page.getByRole('dialog').waitFor();
  }
  report.checks.push({role,pending_state_fixture:'BROWSER_ONLY',review_dialog_opened:true,business_mutations:0});
  await context.close();
 }
 // Check the advice is internal-only, and has the actual frozen policy clause.
 for(const role of ['customer','support','approver']) {
  const {token}=await(await fetch('http://127.0.0.1:18224/api/auth/demo-role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})})).json();
  const response=await fetch(`http://127.0.0.1:18224/api/cases/${caseId}`,{headers:{Authorization:`Bearer ${token}`}});
  assert.equal(response.status,200);const value=await response.json();
  if(role==='customer') assert.equal(value.agent_advice,undefined);
  else {assert.equal(value.agent_advice.candidate.options[0].lines[0].amount_cents,4900);assert.ok(value.agent_advice.policies.some(p=>p.id==='POL14'));}
 }
 report.status='PASSED';
} catch(error) {report.status='FAILED';report.error=String(error.message).slice(0,1200);process.exitCode=1;}
finally {await browser.close();await writeFile(`${dir}/report.json`,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));}
