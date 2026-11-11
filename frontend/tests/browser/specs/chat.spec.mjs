import {expect} from '@playwright/test';
import {chatId, signIn, test} from './api-fixture.mjs';

test('fixture: customer retries one message, confirms the order, adds photos and reads a public support reply', async ({page, fixtureApi}) => {
  fixtureApi.chatAbortFirst = true;
  await signIn(page, 'customer');
  await expect(page.getByRole('heading', {name: '你好，需要什么售后帮助？'})).toBeVisible();
  await page.getByRole('button', {name: '新对话'}).click();
  const composer = page.getByRole('textbox', {name: '描述售后问题'});
  await composer.fill('我的退款到账了吗？');
  await page.getByRole('button', {name: /^发\s*送$/}).click();
  await expect(page.getByRole('button', {name: '重试发送'})).toBeVisible();
  await expect(composer).toHaveValue('我的退款到账了吗？');
  await page.getByRole('button', {name: '重试发送'}).click();
  await expect(page.getByText('我的退款到账了吗？', {exact: true})).toHaveCount(1);
  expect(fixtureApi.chatPosts[1]).toEqual(fixtureApi.chatPosts[0]);
  expect(fixtureApi.chatPosts[0].id).toBe(fixtureApi.chatPosts[0].key);
  await page.locator('.chat-order').filter({hasText: '订单 A1001'}).getByRole('button', {name: '确认订单和商品'}).click();
  await expect(page.getByText('这笔订单的退款账本中未查到执行记录。', {exact: true})).toBeVisible();
  expect(fixtureApi.chatContext[0]).toMatchObject({order_id: 'A1001', line_ids: ['L01'], new_issue: false});
  await composer.fill('再补充两张照片。');
  await page.getByLabel('添加照片', {exact: true}).setInputFiles(['front.png', 'back.png'].map((name) => ({
    name, mimeType: 'image/png', buffer: Buffer.from('controlled fixture'),
  })));
  await page.getByRole('button', {name: /^发\s*送$/}).click();
  await expect(page.getByText('已收到照片。', {exact: true})).toBeVisible();
  expect(fixtureApi.chatPosts.at(-1).names).toEqual(['front.png', 'back.png']);
  await page.reload();
  await expect(page.getByText('再补充两张照片。', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '确认订单和商品'})).toHaveCount(0);
  await page.getByRole('button', {name: '查看照片 · front.png'}).click();
  await expect(page.getByRole('img', {name: 'front.png'})).toHaveAttribute('src', /^blob:/);
  expect(fixtureApi.calls.some((call) => call.path.endsWith('/attachments/photo-0/content') && call.role === 'customer')).toBe(true);

  await page.getByRole('button', {name: '退出'}).click();
  await signIn(page, 'support');
  await page.getByRole('button', {name: '客户对话', exact: true}).click();
  await page.getByRole('button', {name: /订单 A1001/}).click();
  await page.getByRole('textbox', {name: '公开回复'}).fill('客服已收到补充内容。');
  await page.getByRole('button', {name: '发送公开回复'}).click();
  await expect(page.getByText('客服已收到补充内容。', {exact: true})).toBeVisible();

  await page.getByRole('button', {name: '退出'}).click();
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByText('客服已收到补充内容。', {exact: true})).toBeVisible();
  expect(fixtureApi.calls.some((call) => call.path.includes('/runs'))).toBe(false);
});

test('fixture: older messages paginate and another order requires explicit confirmation', async ({page, fixtureApi}) => {
  fixtureApi.chatCreated = true;
  for (let sequence = 1; sequence <= 52; sequence++) {
    fixtureApi.chatMessages.push({
      id: `seeded-${sequence}`, conversation_id: chatId, sequence, role: 'customer',
      text: `历史消息 ${sequence}`, created_at: '2026-11-10T04:00:00Z', attachments: [], cards: [],
    });
  }
  await signIn(page, 'customer');
  await page.goto(`/?conversation=${chatId}`);
  await expect(page.getByText('历史消息 1', {exact: true})).toBeVisible();
  await expect(page.getByText('历史消息 52', {exact: true})).toHaveCount(0);
  await page.getByRole('button', {name: '加载更多消息'}).click();
  await expect(page.getByText('历史消息 52', {exact: true})).toBeVisible();
  expect(fixtureApi.chatDetails).toContain(50);

  const composer = page.getByRole('textbox', {name: '描述售后问题'});
  const send = page.getByRole('button', {name: /^发\s*送$/});
  await composer.fill('先查询第一笔订单退款');
  await send.click();
  await page.locator('.chat-order').filter({hasText: '订单 A1001'}).getByRole('button', {name: '确认订单和商品'}).click();
  await expect(page.getByText('这笔订单的退款账本中未查到执行记录。', {exact: true})).toBeVisible();
  await composer.fill('我还想处理另一笔订单 A1002');
  await send.click();
  await page.locator('.chat-order').filter({hasText: '订单 A1002'}).getByRole('button', {name: '确认订单和商品'}).click();
  const confirmation = page.getByRole('dialog', {name: '确认开始另一项售后'});
  await expect(confirmation).toBeVisible();
  expect(fixtureApi.chatContext).toHaveLength(1);
  await confirmation.getByRole('button', {name: '确认开始'}).click();
  await expect(confirmation).toHaveCount(0);
  expect(fixtureApi.chatContext[1]).toMatchObject({order_id: 'A1002', line_ids: ['L02'], new_issue: true});
});
