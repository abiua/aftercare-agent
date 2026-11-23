import { expect } from '@playwright/test';
import { signIn, test } from './api-fixture.mjs';

test('tester aligns four measurements and drills into a batch with a known window', async ({page,fixtureApi}) => {
  void fixtureApi;
  const id='e4a63880-71b1-4cfc-aeb9-a665f8ac3b15';
  const batch={id,label:'离线汇总',start:1800000000,end:1800000010,state:'completed'};
  await page.route('**/api/operations/**', route=>{
    const url = new URL(route.request().url());
    if(url.pathname.endsWith('/batches')) return route.fulfill({json:{items:[batch],undated:['历史批次 A']}});
    const picked = url.searchParams.get('batch')===id;
    const start = picked ? batch.start : batch.start-50;
    const series = Object.fromEntries([['cpu_percent',20],['api_p95_ms',60],['queue_messages',4],['benchmark_known_usd',.12]]
      .map(([key,n])=>[key,[[start,n],[start+5,n/2],[start+10,n]]]));
    return route.fulfill({json:{start,end:start+10,step:5,batch:picked?batch:null,series,resource_scope:'API 所在主机'}});
  });
  await signIn(page,'tester','/?view=operations');
  await expect(page.getByRole('heading',{name:'系统运行总览'})).toBeVisible();
  await page.getByRole('combobox',{name:'评测批次',exact:true}).click();
  await page.getByText(/· 离线汇总$/).click();
  await expect(page.getByRole('heading',{name:'批次运行时段'})).toBeVisible();
  await expect(page.getByRole('link',{name:'查看批次结果'})).toHaveAttribute('href','?eval=jobs&run_batch='+id);
  const cursor = page.getByRole('slider',{name:'对照时刻'});
  await cursor.focus();
  await cursor.press('ArrowLeft');
  for (const value of ['10 %','30 ms','2 条','0.06 USD']) await expect(page.getByText(value,{exact:true})).toBeVisible();
  await page.getByRole('combobox',{name:'资源占用指标'}).click();
  await page.getByText('系统内存',{exact:true}).click();
  await expect(page.getByText('此时段暂无采样')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading',{name:'批次运行时段'})).toBeVisible();
});

test('support has no operations navigation', async ({page,fixtureApi})=>{
  void fixtureApi;
  await signIn(page,'support','/?view=operations');
  await expect(page.getByRole('button',{name:'系统运行总览'})).toHaveCount(0);
});
