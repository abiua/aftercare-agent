import assert from 'node:assert/strict';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../frontend/tests/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const output='reports/tau-official-browser';
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
try {
 const page=await browser.newPage({viewport:{width:1560,height:1100}});
 page.setDefaultTimeout(25000);
 await page.goto('http://127.0.0.1:5225/?eval=campaigns');
 await page.getByRole('button',{name:'以测试员身份进入',exact:true}).click();
 await page.getByRole('tab',{name:'官方 τ²-Bench',exact:true}).click();
 await page.getByText('τ²-Bench Retail 官方测试集',{exact:true}).waitFor();
 await page.getByText('完整测试集 · 40 道任务 · 每组每题 1 次',{exact:true}).waitFor();
 assert.equal(await page.getByRole('tab',{name:'用例库',exact:true}).count(),0);
 assert.equal(await page.getByRole('tab',{name:'实验记录',exact:true}).count(),0);
 const body=await page.locator('body').innerText();
 assert.ok(body.includes('gpt-6.1-sol') && body.includes('强单 Agent') && body.includes('多 Agent'));
 assert.ok(!body.includes('DAMAGE-ORDINARY') && !body.includes('开发参考达标'));
 const source=JSON.parse(await readFile('evidence/tau_official/latest.json','utf8'));
 let technicalRetryVisible=false;
 if(source.repair_directory) {
  await page.getByText('含 1 条 API 异常后的补跑，首次异常记录保留',{exact:true}).waitFor();
  for(const [label,name,count] of [['下载首次运行报告','primary',120],['下载补跑报告','repair',1]]) {
   const pending=page.waitForEvent('download');
   await page.getByRole('button',{name:label,exact:true}).click();
   const file=`${output}/${name}.json`;
   await (await pending).saveAs(file);
   const report=JSON.parse(await readFile(file,'utf8'));
   assert.equal(report.rows.length,count);
   assert.ok(report.rows.every(row=>!('official_components' in row) && !('private_trace' in row)));
  }
  technicalRetryVisible=true;
 }
 await page.screenshot({path:`${output}/official-report.png`,fullPage:true});
 await writeFile(`${output}/report.json`,JSON.stringify({official_report_visible:true,all_40_tasks:true,legacy_tabs_removed:true,models_visible:true,technical_retry_visible:technicalRetryVisible,checked_at:new Date().toISOString()},null,2));
} finally {await browser.close();}
