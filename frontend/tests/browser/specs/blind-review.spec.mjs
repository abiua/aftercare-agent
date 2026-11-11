import {expect} from '@playwright/test';
import {signIn, test} from './api-fixture.mjs';

function seed(state) {
  state.blindReview = {
    posts: [], task: {id: 'blind-task', version: 1, status: 'BLIND', case_label: '盲审测试样例',
      input_sha256: 'a'.repeat(64), initial: null, exposure: null, labels: null,
      input: {schema_version: 'chat-blind-input-v1', intent: 'damaged', policy_coverage: 'SCOPED_ONLY', policy_context: null, messages: [{id: 'm1', sequence: 1, text: '杯口有裂纹，请问可以怎么处理？'}],
        images: [{id: 'image-1', message_id: 'm1', sha256: 'b'.repeat(64), size: 100, content_type: 'image/png'}],
        facts: [{id: 'ORDER_LINE:L01', kind: 'ORDER_LINE', trust: 'VERIFIED_RECORD', data: {line_id: 'L01', title: '测试杯', paid_cents: 825}},
          {id: 'REVIEW:r1', kind: 'REVIEW', trust: 'VERIFIED_RECORD', data: {decision: 'APPROVED', material_ids: ['m1'], materials_version: 1, effective: true}},
          {id: 'OBSERVATION:o1', kind: 'OBSERVATION', trust: 'UNVERIFIED_OBSERVATION', data: {summary: '疑似可见裂纹'}}],
        policies: [{id: 'POL01', version: '1.0', title: '破损条款', text: '材料经核对后可以办理。', parameters: {}, conditions: {priority: 10}, selected_for: ['derived-item-must-not-display']}]}},
    candidates: [{id: 'hidden-candidate', status: 'AVAILABLE', unresolved: ['仍需确认的原始候选事项'], options: [{id: 'candidate-option', action: 'REFUND',
      lines: [{line_id: 'L01', amount_cents: 825}], policy_ids: ['POL01', 'POL-UNKNOWN'], evidence_ids: ['ORDER_LINE:L01', 'FACT:unknown'], required_evidence: [],
      rationale: '候选原始理由', alternatives: ['候选原始备选'],
      policy_references: [{id: 'POL01', status: 'RESOLVED', resolved_ids: ['POL01@1.0']}, {id: 'POL-UNKNOWN', status: 'UNRESOLVED', resolved_ids: []}],
      evidence_references: [{id: 'ORDER_LINE:L01', status: 'RESOLVED', resolved_ids: ['ORDER_LINE:L01']},
        {id: 'FACT:unknown', status: 'UNRESOLVED', resolved_ids: []}]}]},
    {id: 'unavailable-candidate', status: 'UNAVAILABLE', unresolved: [], options: []}],
  };
  return state.blindReview;
}

async function open(page) {
  await page.getByRole('button', {name: '盲审测试样例'}).click();
  await expect(page.getByText('杯口有裂纹，请问可以怎么处理？')).toBeVisible();
}

test('fixture: seal before explicit reveal, reopen immutable initial, and label separately', async ({page, fixtureApi}) => {
  const state = seed(fixtureApi);
  const before = fixtureApi.calls.length;
  await signIn(page, 'tester', '/?eval=blind&eval_experiment=blind-must-not-open&eval_case=hidden-case&eval_repeat=1');
  await open(page);
  expect(fixtureApi.calls.slice(before).some(({path}) => path.includes('/evaluation/experiments'))).toBe(false);
  await expect(page.getByText('待核实的图片观察')).toBeVisible();
  await expect(page.getByText('材料经核对后可以办理。')).toBeVisible();
  await expect(page.getByText('材料审核记录 · REVIEW:r1', {exact: true}).first()).toBeVisible();
  await expect(page.getByText('材料审核已通过 · 当前有效')).toBeVisible();
  await expect(page.getByText('此快照只保留当时选取的条款，无法核对未归档的条款。')).toBeVisible();
  await page.getByRole('button', {name: '查看适用范围与参数'}).click();
  await expect(page.getByText(/"priority": 10/)).toBeVisible();
  await expect(page.getByText(/derived-item-must-not-display|候选原始理由/)).toHaveCount(0);
  await expect(page.getByText('已揭示的候选')).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
  await page.getByRole('button', {name: '查看消息 1 的原图 1'}).click();
  await expect(page.getByRole('img', {name: '消息 1 的原图 1'})).toHaveAttribute('src', /^blob:/);
  await page.getByRole('radio', {name: '无法判断', exact: true}).check();
  await page.getByRole('textbox', {name: '判断说明'}).fill('原图证据仍需核对。');
  await page.getByRole('button', {name: '封存初判'}).click();
  await page.getByRole('button', {name: '确认封存'}).click();
  await expect(page.getByText('已封存的独立判断')).toBeVisible();
  expect(state.posts[0]).toMatchObject({stage: 'initial', key: expect.any(String), body: {
    expected_version: 1, judgment: 'UNDETERMINED', acceptable_plans: [], notes: '原图证据仍需核对。',
  }});
  await expect(page.getByText('已揭示的候选')).toHaveCount(0);

  await page.reload();
  await open(page);
  await expect(page.getByRole('button', {name: '封存初判'})).toHaveCount(0);
  await expect(page.getByText('原图证据仍需核对。')).toBeVisible();
  await page.getByRole('button', {name: '查看候选', exact: true}).click();
  await expect(page.getByRole('dialog', {name: '揭示助手候选'})).toBeVisible();
  expect(state.posts).toHaveLength(1);
  await page.getByRole('button', {name: '确认揭示'}).click();
  await expect(page.getByText('已揭示的候选')).toBeVisible();
  await expect(page.getByText('理由 · 候选原始理由')).toBeVisible();
  await expect(page.getByText('候选原始备选')).toBeVisible();
  await expect(page.getByText('仍需确认的原始候选事项')).toBeVisible();
  await expect(page.getByRole('listitem').filter({hasText: 'POL-UNKNOWN'})).toContainText('未匹配冻结记录');
  await expect(page.getByRole('listitem').filter({hasText: 'FACT:unknown'})).toContainText('未匹配冻结记录');
  await expect(page.getByRole('listitem').filter({hasText: 'POL01@1.0'})).toContainText('已匹配冻结记录');
  expect(state.posts[1]).toMatchObject({stage: 'reveal', key: expect.any(String), body: {expected_version: 2}});
  await expect(page.getByRole('button', {name: '保存后置复核'})).toBeDisabled();
  const groups = page.getByRole('group', {name: '这个候选是否符合你能接受的处理方式？'});
  await groups.nth(0).getByRole('radio', {name: '可以接受'}).check();
  await expect(groups.nth(1).getByRole('radio', {name: '可以接受'})).toBeDisabled();
  await groups.nth(1).getByRole('radio', {name: '无法判断'}).check();
  await page.getByRole('button', {name: '保存后置复核'}).click();
  await expect(page.getByText(/后置复核已记录 ·/)).toBeVisible();
  expect(state.posts[2]).toMatchObject({stage: 'labels', body: {expected_version: 3,
    items: [{candidate_id: 'hidden-candidate', decision: 'ACCEPTABLE'}, {candidate_id: 'unavailable-candidate', decision: 'UNDETERMINED'}]}});
  expect(state.task.initial.payload.notes).toBe('原图证据仍需核对。');
});

test('fixture: v2 blind review presents structured human material observations', async ({page, fixtureApi}) => {
  const state = seed(fixtureApi);
  state.task.input.schema_version = 'chat-blind-input-v2';
  state.task.input.facts[1].data.observations = [
    {source: 'HUMAN_MATERIAL_REVIEW', material_id: 'material-1', finding: 'VISIBLE_DAMAGE',
      line_id: 'L01', affected_quantity: 2, material_sha256: 'b'.repeat(64),
      original_attachments: [{attachment_id: 'image-1', message_id: 'm1', sha256: 'b'.repeat(64)}]},
    {source: 'HUMAN_MATERIAL_REVIEW', material_id: 'material-2', finding: 'UNCERTAIN',
      material_sha256: 'c'.repeat(64), original_attachments: []},
  ];
  await signIn(page, 'tester', '/?eval=blind');
  await open(page);
  await expect(page.getByText('人工记录的照片观察')).toBeVisible();
  await expect(page.getByRole('listitem').filter({hasText: 'material-1'})).toContainText(
    '可见破损 · 商品行 L01 · 该商品行受影响总数 2 件');
  await expect(page.getByRole('listitem').filter({hasText: 'material-2'})).toContainText('无法判断');
  await expect(page.getByText('已揭示的候选')).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
});

test('fixture: independent refund range uses exact cents and stale seal cannot reveal', async ({page, fixtureApi}) => {
  const state = seed(fixtureApi);
  state.task.input.policy_coverage = 'COMPLETE_CATALOGUE';
  state.task.input.policy_context = {timestamps: {decision_at: '2026-11-11T08:00:00Z'}};
  await signIn(page, 'tester');
  await page.getByRole('tab', {name: '独立盲审'}).click();
  await open(page);
  await expect(page.getByText('包含冻结时的完整条款目录。')).toBeVisible();
  await page.getByRole('button', {name: '查看条款判断所用的原始业务背景'}).click();
  await expect(page.getByText(/"decision_at": "2026-11-11T08:00:00Z"/)).toBeVisible();
  await page.getByRole('radio', {name: '可以判断'}).check();
  await page.getByRole('button', {name: '添加可接受方案'}).click();
  await page.getByRole('combobox', {name: '方案 1 的动作'}).click();
  await page.getByRole('option', {name: '退款', exact: true}).click();
  await page.getByRole('combobox', {name: '方案 1 的商品'}).click();
  await page.getByRole('option', {name: '测试杯 · 商品行 L01'}).click();
  await page.getByRole('textbox', {name: '商品行 L01 最低退款金额'}).fill('5.01');
  await page.getByRole('textbox', {name: '商品行 L01 最高退款金额'}).fill('4.99');
  await expect(page.getByRole('button', {name: '封存初判'})).toBeDisabled();
  await page.getByRole('textbox', {name: '商品行 L01 最高退款金额'}).fill('8.25');
  await page.getByRole('checkbox', {name: /ORDER_LINE:L01/}).check();
  await page.getByRole('combobox', {name: '条款 POL01 第 1.0 版的适用性'}).click();
  await page.getByRole('option', {name: '适用', exact: true}).click();
  await page.getByRole('button', {name: '封存初判'}).click();
  state.conflict = true;
  const confirm = page.getByRole('dialog', {name: '封存独立判断'});
  await confirm.getByRole('button', {name: '确认封存'}).click();
  await expect(confirm.getByText('任务阶段已更新，请刷新后查看现有记录。')).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(state.posts[0].body).toMatchObject({expected_version: 1, judgment: 'DECIDABLE',
    acceptable_plans: [{action: 'REFUND', lines: [{line_id: 'L01', amount_min_cents: 501, amount_max_cents: 825}], required_evidence: []}],
    required_fact_ids: ['ORDER_LINE:L01'], policy_assessments: [{policy_id: 'POL01', version: '1.0', status: 'APPLICABLE'}],
  });
  await expect(page.getByText('已揭示的候选')).toHaveCount(0);
  expect(state.task.initial).toBeNull();
});

for (const fails of [false, true]) {
  test(`fixture: blind route isolates experiment reads when capability ${fails ? 'fails' : 'is disabled'}`, async ({page, fixtureApi}) => {
    fixtureApi.blindCapabilityError = fails;
    const before = fixtureApi.calls.length;
    await signIn(page, 'tester', '/?eval=blind&eval_experiment=blind-must-not-open&eval_case=hidden-case&eval_repeat=1');
    await expect(page.getByText(fails ? '暂时无法检查盲审是否可用' : '当前环境未开放独立盲审')).toBeVisible();
    await expect(page.getByRole('tab', {name: '独立盲审'})).toHaveAttribute('aria-selected', 'true');
    expect(fixtureApi.calls.slice(before).filter(({path}) => path.startsWith('/api/evaluation/')).map(({path}) => path))
      .toEqual(['/api/evaluation/capabilities']);
    await page.getByRole('tab', {name: '实验与能力指标'}).click();
    await expect(page.getByRole('heading', {name: '实验记录'})).toBeVisible();
    await expect(page).toHaveURL(/\?eval=experiments$/);
    await expect.poll(() => fixtureApi.calls.slice(before).some(({path}) => path === '/api/evaluation/experiments')).toBe(true);
    expect(fixtureApi.calls.slice(before).some(({path}) => path.includes('blind-must-not-open'))).toBe(false);
  });
}
