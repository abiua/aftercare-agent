import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../frontend/tests/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const name=process.argv[2]; assert.ok(['ordinary','complex','failure'].includes(name));
const dir='reports/step5-basic-20261113';
const fixture=JSON.parse(await readFile(`${dir}/${name}.json`,'utf8'));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const result={scenario:name,roles:{},errors:[]};
try{
 for(const [role,label] of [['customer','客户'],['support','客服']]){
  const ctx=await browser.newContext({viewport:{width:1500,height:1100}});
  const page=await ctx.newPage();page.setDefaultTimeout(20000);
  page.on('pageerror',e=>result.errors.push(String(e)));
  await page.goto(`http://127.0.0.1:5225/?${role==='customer'?'conversation='+fixture.conversation_id:'case='+fixture.case_id}`);
  await page.getByRole('button',{name:`以${label}身份进入`,exact:true}).click();
  await page.waitForTimeout(1000);
  if(role==='support') await page.getByRole('tab',{name:'Agent 建议与客服审核',exact:true}).click();
  const text=await page.locator('body').innerText();
  await writeFile(`${dir}/${name}-${role}.txt`,text);
  assert.ok(text.includes(fixture.order_id));
  if(name==='failure'&&role==='customer') assert.ok(text.includes('查询超时')&&text.includes('已转客服'));
  await page.screenshot({path:`${dir}/${name}-${role}.png`,fullPage:true});
  result.roles[role]={loaded:true};
  if(role==='support'){
   await page.getByRole('tab',{name:'调查过程与运行记录',exact:true}).click();await page.waitForTimeout(600);
   await page.screenshot({path:`${dir}/${name}-trace.png`,fullPage:true});
   await writeFile(`${dir}/${name}-trace.txt`,await page.locator('body').innerText());
  }
  await ctx.close();
 }
 assert.equal(result.errors.length,0);
 result.status='PASSED';
} catch(e){result.status='FAILED';result.error=String(e);throw e;}
finally{await writeFile(`${dir}/${name}-browser.json`,JSON.stringify(result,null,2));await browser.close();}
console.log(JSON.stringify(result));
