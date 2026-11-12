import assert from 'node:assert/strict';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../frontend/tests/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const output='reports/official-and-handoff-browser';
await mkdir(output,{recursive:true});
const reference=JSON.parse(await readFile('reports/p6-handoff-http-20261112.json','utf8'));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const report={};
try{
 const page=await browser.newPage({viewport:{width:1560,height:1100}});
 page.setDefaultTimeout(20000);
 await page.goto('http://127.0.0.1:5225/?eval=campaigns');
 await page.getByRole('button',{name:'以测试员身份进入',exact:true}).click();
 await page.getByRole('tab',{name:'官方 τ²-Bench',exact:true}).click();
 await page.getByText('τ²-Bench Retail 官方测试集',{exact:true}).waitFor();
 await page.screenshot({path:`${output}/campaigns.png`,fullPage:true});
 report.campaigns=true;
 await page.getByRole('tab',{name:'问题与回归',exact:true}).click();
 await page.getByText('OCR把Issue识别为lssue',{exact:true}).waitFor();
 await page.screenshot({path:`${output}/bad-cases.png`,fullPage:true});report.bad_cases=true;
 const context=await browser.newContext({viewport:{width:1500,height:1100}});
 const customer=await context.newPage();
 await customer.goto('http://127.0.0.1:5225/');
 await customer.getByRole('button',{name:'以客户身份进入',exact:true}).click();
 const back=customer.getByRole('button',{name:'继续处理订单 O9971-P6',exact:true});
 await back.click();
 await customer.getByText(reference.handoff_message,{exact:true}).waitFor();
 assert.ok(customer.url().includes(reference.conversation_id));
 await customer.screenshot({path:`${output}/handoff.png`,fullPage:true});
 report.original_thread_found=true;report.clear_failure_message=true;
 await writeFile(`${output}/report.json`,JSON.stringify(report,null,2));
}catch(error){report.error=String(error);await writeFile(`${output}/failure.json`,JSON.stringify(report,null,2));throw error;}
finally{await browser.close();}
