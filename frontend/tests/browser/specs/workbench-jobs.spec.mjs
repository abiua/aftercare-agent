import { expect } from '@playwright/test';
import { signIn, test } from './api-fixture.mjs';

test('preview is free and a confirmed batch automatically shows its metrics', async ({page, fixtureApi}) => {
  void fixtureApi;
  const base = '/api/evaluation/campaign-jobs';
  const posts = [];
  let job;
  let polls = 0;
  await page.route('**/api/evaluation/campaign-jobs**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === base + '/catalog') return route.fulfill({json: {
      tasks:[{id:'5',label:'交换商品'}], architectures:{S1:'强单 Agent',M2:'低成本执行＋强审查'},revision:'revision123456',
    }});
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      posts.push(path);
      if (path.endsWith('/preview')) {
        expect(body).toEqual({task_ids:['5'],architectures:['S1','M2'],max_usd:0.4,max_calls:7});
        job = {...body, id:'batch-fixture',revision:'revision123456',commit:'fixturecommit',snapshot:'fixturesnapshot',created:new Date().toISOString(),
          missing:[],state:'preview',counts:{},planned:2,finished:0};
      } else {
        expect(path).toBe(base + '/batch-fixture/start');
        expect(body.confirm_paid).toBe(true);
        job = {...job, state:'running'};
      }
      return route.fulfill({json:job});
    }
    if (job?.state === 'running' && ++polls >= 2) {
      job = {...job, state:'partial',finished:2,counts:{completed:1,budget_exhausted:1},result:{
        id:job.id,label:'离线样例',revision:job.revision,split:'test',tasks:1,trials:1,repairs:0,groups:[],commit:job.commit,
      }};
    }
    return route.fulfill({json:{items:job ? [job] : []}});
  });
  await signIn(page, 'tester', '/?eval=jobs');
  await page.getByRole('combobox',{name:'评测任务'}).click();
  await page.getByText('#5 交换商品',{exact:true}).click();
  await page.keyboard.press('Escape');
  await page.getByRole('combobox',{name:'Agent 架构'}).click();
  await page.getByText('M2 · 低成本执行＋强审查',{exact:true}).click();
  await page.keyboard.press('Escape');
  await page.getByLabel('整批美元上限').fill('0.4');
  await page.getByLabel('整批模型调用上限').fill('7');
  await page.getByRole('button',{name:'生成免费预览'}).click();
  await expect(page.getByText('1 题 × 2 个架构 = 2 次')).toBeVisible();
  const launch = page.getByRole('button',{name:'启动付费评测'});
  await expect(launch).toBeDisabled();
  expect(posts).toEqual([base + '/preview']);
  await page.getByRole('checkbox',{name:'确认按本批预算调用付费模型'}).check();
  await launch.click();
  await expect(page.getByRole('heading',{name:'本批评测结果'})).toBeVisible({timeout:12000});
  await expect(page.getByText('已完成 1 · 预算用尽 1')).toBeVisible();
  expect(posts).toEqual([base + '/preview', base + '/batch-fixture/start']);
  await page.reload();
  await expect(page.getByRole('heading',{name:'本批评测结果'})).toBeVisible();
});
