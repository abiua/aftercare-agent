import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatProviderTokens, formatSupplementalCost, supplementalUsageMessage } from '../src/supplementalUsagePresentation.ts';

const base = {
  result_id: 'result-one', as_of: '2026-11-10T10:00:00Z', status: 'AVAILABLE',
  unavailable_reason: null, unmatched_attempt_count: 0, unmatched_snapshot_attempt_count: 0, attempts: [],
};

test('distinguishes unreadable evidence, no paired differences and unmatched calls', () => {
  assert.deepEqual(supplementalUsageMessage({...base, status: 'UNAVAILABLE', unavailable_reason: 'SCHEMA_NOT_FOUND'}), {
    title: '补充用量审计证据不可用', detail: '本次运行的用量账本已无法读取。',
  });
  assert.deepEqual(supplementalUsageMessage(base), {title: '未发现可配对调用的记录差异', detail: undefined});
  assert.deepEqual(supplementalUsageMessage({...base, unmatched_attempt_count: 2, unmatched_snapshot_attempt_count: 1}), {
    title: '存在无法配对的调用记录',
    detail: '当前账本有 2 条调用记录无法与原快照配对。原快照有 1 条调用记录无法与当前账本配对。',
  });
  assert.deepEqual(supplementalUsageMessage({...base, attempts: [{}], unmatched_snapshot_attempt_count: 1}), {
    title: '发现 1 次已配对调用的记录差异', detail: '原快照有 1 条调用记录无法与当前账本配对。',
  });
});

test('keeps late decimal cost exact and distinguishes unknown from an explicit zero', () => {
  assert.equal(formatSupplementalCost('0.00002'), '$0.00002');
  assert.equal(formatSupplementalCost('0.000000000000000001'), '$0.000000000000000001');
  assert.equal(formatSupplementalCost('0'), '$0');
  assert.equal(formatSupplementalCost(null), '未知');
});

test('lists only reported provider counters, preserving explicit zero and separate cache counters', () => {
  assert.deepEqual(formatProviderTokens(null), []);
  assert.deepEqual(formatProviderTokens({input_tokens: 14, output_tokens: 0, total_tokens: -1,
    input_token_details: {cache_read: 3, cache_creation: 1},
    output_token_details: {reasoning: 0, audio: 1}, unknown_provider_payload: 99,
  }), ['输入 14', '输出 0', '缓存读取 3', '缓存写入 1', '推理 0', '输出音频 1']);
});
