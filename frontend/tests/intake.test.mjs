import assert from 'node:assert/strict';
import { test } from 'node:test';
import { intakeConfirmRequest, intakeInputKey, intakeMessage, intakePreviewRequest, intakeSource } from '../src/intake.ts';

const draft = {message: '  收到的商品已经破损  '};
const order = {id: 'A1001', version: 3, status: 'DELIVERED'};
const result = {
  status: 'READY', required: [], order, order_candidates: [order],
  intent: 'damaged', intent_candidates: ['damaged'], classifier_source: 'rules', reason: null,
};

test('starts with text-only rules preview and localizes actual classifier sources', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(intakePreviewRequest(draft))), {
    message: '收到的商品已经破损', classifier: 'rules',
  });
  assert.equal(intakeSource('rules'), '按规则识别');
  assert.equal(intakeSource('confirmed'), '根据你的选择');
  assert.equal(intakeSource('model'), '由模型识别');
  assert.equal(intakeSource('model_unavailable'), '模型暂不可用，请手动选择');
  assert.match(intakeMessage('MULTIPLE_ORDERS'), /本人订单/);
  assert.match(intakeMessage('INTAKE_CUSTOMER_REQUIRED'), /本人客户账号/);
  assert.equal(intakeMessage('__proto__'), undefined);
});

test('refuses to build a submission while owner order or intent is ambiguous', () => {
  const bind = (next) => ({inputKey: intakeInputKey(draft), result: next});
  assert.equal(intakeConfirmRequest(draft, bind({...result, status: 'CLARIFICATION_REQUIRED', required: ['order'], order: null})), undefined);
  assert.equal(intakeConfirmRequest(draft, bind({...result, status: 'CLARIFICATION_REQUIRED', required: ['intent'], intent: null})), undefined);
  assert.equal(intakeConfirmRequest(draft, bind({...result, status: 'READY', order: {...order, version: 0}})), undefined);
  assert.equal(intakeConfirmRequest(draft), undefined);
});

test('binds confirmation to the reviewed order, intent and version; repeated input stays identical', () => {
  const preview = {inputKey: intakeInputKey(draft), result};
  const payload = intakeConfirmRequest(draft, preview);
  assert.deepEqual(payload, {
    message: '收到的商品已经破损', order_id: 'A1001', intent: 'damaged', expected_version: 3, confirmed: true,
  });
  assert.deepEqual(intakeConfirmRequest({message: '收到的商品已经破损'}, preview), payload);
  assert.equal(intakeConfirmRequest({...draft, message: '我想确认款项进度'}, preview), undefined);
  assert.equal(intakeConfirmRequest({...draft, orderId: 'A1002'}, preview), undefined);
  assert.equal(intakeConfirmRequest({...draft, intent: 'refund_status'}, preview), undefined);
  const explicit = {...draft, orderId: 'A1002', intent: 'damaged'};
  assert.equal(intakeConfirmRequest(explicit, {inputKey: intakeInputKey(explicit), result}), undefined);
});
