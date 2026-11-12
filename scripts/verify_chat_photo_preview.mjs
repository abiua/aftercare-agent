import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../frontend/tests/browser/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const dir='reports/chat-photo-preview-20261112'; await mkdir(dir,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const report={status:'RUNNING',checks:[]};
let page;
try {
 page=await browser.newPage({viewport:{width:1440,height:1000}});
 for (const [id,filename] of [['1da9113a-36e7-4d18-aa17-942847c0e9b5','before.png'],['0b79bd2c-0445-4af2-81c7-18407d2e7d88','synthetic-cracked-mug.png']]) {
  await page.goto(`http://127.0.0.1:5224/?conversation=${id}`);
  if(filename==='before.png') await page.getByRole('button',{name:'以客服身份进入',exact:true}).click();
  await page.getByRole('button',{name:`查看照片 · ${filename}`,exact:true}).first().click();
  const modal=page.getByRole('dialog'); await modal.waitFor();
  const img=modal.getByRole('img',{name:`原图 · ${filename}`,exact:true});
  await img.evaluate(e=>e.decode());
  await page.waitForFunction(() => [...document.querySelectorAll('.chat-photo-original')].some(e=>e.getBoundingClientRect().height>200));
  const shape=await img.evaluate(e=>({width:e.naturalWidth,height:e.naturalHeight,displayedWidth:e.getBoundingClientRect().width,displayedHeight:e.getBoundingClientRect().height}));
  assert.ok(shape.displayedHeight>200);
  if(filename==='before.png') {assert.equal(shape.width,8);await modal.getByText(/原图尺寸较小/).waitFor();}
  else assert.ok(shape.width>300);
  await page.screenshot({path:`${dir}/${filename}`,fullPage:false,animations:'disabled'});
  await modal.getByRole('button',{name:'关闭预览',exact:true}).click();
  await page.getByRole('button',{name:`放大照片 · ${filename}`,exact:true}).first().click();
  await page.getByRole('dialog').waitFor();
  await page.getByRole('dialog').getByRole('button',{name:'关闭预览',exact:true}).click();
  report.checks.push({filename,...shape,reopen:true});
 }
 await page.goto('http://127.0.0.1:5224/?conversation=1da9113a-36e7-4d18-aa17-942847c0e9b5');
 await page.getByLabel('公开回复',{exact:true}).waitFor();
 const route='**/attachments/*/content';
 await page.route(route,r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:{message:'图片暂时不可用'}})}));
 await page.getByRole('button',{name:'查看照片 · supplement.png',exact:true}).first().click();
 await page.getByText('图片暂时不可用',{exact:true}).waitFor();
 await page.unroute(route);
 await page.getByRole('button',{name:'查看照片 · supplement.png',exact:true}).first().click();
 await page.getByRole('dialog').getByRole('img',{name:'原图 · supplement.png',exact:true}).evaluate(e=>e.decode());
 report.checks.push({scenario:'browser_injected_503',error_visible:true,retry_actual_image:true});
 report.status='PASSED';
} catch(error) {report.status='FAILED';report.error=String(error.message).slice(0,1200);report.page_text=await page.locator('body').innerText();process.exitCode=1;}
finally {await browser.close();await writeFile(`${dir}/report.json`,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));}
