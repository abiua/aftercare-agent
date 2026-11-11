import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createHash } from 'node:crypto';
import { summarizeEvaluation, describeThresholds, evaluationFeatures } from '../src/evaluationPresentation.ts';
import { evaluationReason, knownEvaluationMessage, recoveryReason } from '../src/evaluationMessages.ts';
import { readEvaluationLocation, writeEvaluationLocation } from '../src/evaluationRoute.ts';
import { reportImportNotice } from '../src/evaluationImport.ts';

test('summarizes business intent, a draft and verified citations separately from the ledger', () => {
  const summary = summarizeEvaluation({
    expected: {
      allowed_draft_actions: ['REFUND'], refund_amount_cents: {min: 35900, max: 35900},
      required_policy_ids: ['POL06', 'POL14'],
      checks: [
        {path: 'status', equals: 'WAITING_INPUT'},
        {path: 'reason', equals: 'MATERIAL_REVIEW_REQUIRED'},
        {path: 'output.missing_materials', equals: ['WRONG_ITEM', 'LABEL']},
      ],
    },
    actual: {
      observed_source: 'live_provider', model_audit_complete: true,
      model_attempts: [{model: 'economy-model', estimated_cost_usd: '0.0012'}, {model: 'economy-model', estimated_cost_usd: '0.0003'}],
      events: {
        e01: {reason: 'WAITING_INPUT', output: {missing_materials: ['WRONG_ITEM', 'LABEL']}},
        e02: {reason: 'WAITING_INPUT', output: {missing_materials: ['LABEL']}},
        e03: {reason: 'ELIGIBLE', output: {action: 'REFUND', policy_ids: ['POL99'], citations: [{id: 'POL06', version: '1.0', quote: '核对商品照片'}]}},
      },
      final_business: {ledger: [], policies: [{id: 'POL06', title: '损坏补证'}]},
    },
    checks: [{path: 'output.action', status: 'PASS'}, {path: 'output.citations', status: 'FAIL'}, {path: 'output.ledger', status: 'UNKNOWN'}],
    trace: [],
  });
  assert.deepEqual(summary.expectedPath, ['先等待客户补充材料', '材料齐全后等待人工审核']);
  assert.deepEqual(summary.expectedMaterials, ['错发商品实物照片', '包裹标签照片']);
  assert.equal(summary.expectedAmount, '¥359.00');
  assert.deepEqual(summary.actualPath, [
    '已请求补充材料：错发商品实物照片、包裹标签照片', '已请求补充材料：包裹标签照片', '已核对处理资格', '已拟定退款草案',
  ]);
  assert.deepEqual(summary.citations, [{id: 'POL06', version: '1.0', title: 'POL06', quote: '核对商品照片'}]);
  assert.equal(summary.calls, 2);
  assert.ok(Math.abs(summary.estimate - 0.0015) < 1e-12);
  assert.deepEqual(summary.keyChecks.map(({label}) => label), ['政策原文引用', '退款执行账本']);
});

test('keeps reference IDs and excerpts without a version outside the actual citations', () => {
  const summary = summarizeEvaluation({
    expected: {required_policy_ids: ['POL08', 'POL12']},
    actual: {events: {e01: {reason: 'STATUS_ONLY', output: {
      policy_ids: ['POL08', 'POL12'], citations: [{id: 'POL08', quote: '缺少版本的摘录'}],
    }}}},
    checks: [], trace: [],
  });
  assert.deepEqual(summary.referencePolicies, ['POL08', 'POL12']);
  assert.deepEqual(summary.citations, []);
});

test('keeps unaudited or partially priced attempts unknown instead of showing a zero cost', () => {
  const complete = {expected: {}, checks: [], trace: []};
  const partial = summarizeEvaluation({...complete, actual: {
    model_audit_complete: true, model_attempts: [{estimated_cost_usd: '0.02'}, {estimated_cost_usd: null}],
  }});
  assert.equal(partial.calls, 2);
  assert.equal(partial.estimate, undefined);
  const missing = summarizeEvaluation({...complete, actual: {model_audit_complete: false, model_attempts: []}});
  assert.equal(missing.calls, undefined);
  assert.equal(missing.estimate, undefined);
  const offline = summarizeEvaluation({...complete, actual: {model_audit_complete: true, model_attempts: []}});
  assert.equal(offline.calls, 0);
  assert.equal(offline.estimate, 0);
});

test('translates raw pending or unknown failure codes and formats explicit zero thresholds', () => {
  assert.equal(evaluationReason('HUMAN_REFERENCE_PENDING'), '案例参考待人工核验');
  assert.equal(evaluationReason('UNRECOGNIZED_CODE'), '此结果需要进一步核对，详情见原始证据。');
  assert.equal(knownEvaluationMessage('__proto__'), undefined);
  assert.deepEqual(describeThresholds({min_coverage: 0.95, max_final_violation_rate: 0, max_total_cost_usd: '0.10', require_live: true}), [
    {label: '最低执行覆盖率', value: '95%'}, {label: '估算总费用上限', value: '$0.1'},
    {label: '最终违规率上限', value: '0%'}, {label: '评测来源', value: '需要真实模型证据'},
  ]);
});

test('restores comparison and case deep links and preserves unrelated query fields', () => {
  const selected = {tab: 'compare', left: 'one', right: 'two', experiment: 'two', caseId: 'AC02-C081', repetition: 2};
  const href = writeEvaluationLocation('https://localhost:5187/?case=legacy#section', selected);
  assert.deepEqual(readEvaluationLocation(new URL(href).search), selected);
  assert.equal(new URL(href).searchParams.get('case'), 'legacy');
  assert.equal(new URL(href).hash, '#section');
  const closed = writeEvaluationLocation(href, {tab: 'compare', left: 'one', right: 'two'});
  assert.equal(new URL(closed).searchParams.has('eval_case'), false);
  assert.equal(readEvaluationLocation('?eval=compare&eval_left=..%2F&eval_case=AC1&eval_repeat=1').caseId, undefined);
  assert.equal(readEvaluationLocation('?eval=compare&eval_left=..%2F').left, undefined);
});

test('keeps new evaluation actions closed on an old backend and respects an explicit denial', () => {
  assert.deepEqual(evaluationFeatures(undefined, {persistent_checkpoints: true}), {
    importEnabled: false, retryEnabled: false, gatesEnabled: false,
  });
  assert.deepEqual(evaluationFeatures({eval_import: false, eval_retry: true}, {eval_import: true, eval_gates: true}), {
    importEnabled: false, retryEnabled: true, gatesEnabled: false,
  });
});

test('explains recovery failures in the run reason and evaluation event without claiming a retry', () => {
  const messages = {
    RECOVERY_CONTEXT_CHANGED: '原运行依赖的证据或配置已变化。请人工核对后，再发起新一轮运行。',
    RECOVERY_CONTEXT_UNBOUND: '原运行缺少可核对的恢复记录。请人工确认后，再发起新一轮运行。',
    REPLAY_VALIDATION_FAILED: '原运行保留的调用结果无法按当前规则核验。请人工核对后，再发起新一轮运行。',
  };
  for (const [code, message] of Object.entries(messages)) {
    assert.equal(recoveryReason(code), message);
    assert.equal(evaluationReason(code), message);
    const summary = summarizeEvaluation({
      expected: {}, checks: [], trace: [],
      actual: {events: {e01: {status: 'NEEDS_REVIEW', reason: code, output: null}}},
    });
    assert.deepEqual(summary.actualPath, [message]);
  }
  assert.equal(recoveryReason('WAITING_INPUT'), undefined);
});

test('hashes the exact UTF-8 report, including Chinese characters and newline formatting', async () => {
  const {prepareReportImport, maxReportBytes} = await import('../src/evaluationImport.ts');
  const content = '{ "name": "售后评测", "results": [] }\r\n';
  const bytes = new TextEncoder().encode(content);
  assert.deepEqual(await prepareReportImport(bytes.buffer, '  离线记录  '), {
    content, origin: '离线记录', sha256: createHash('sha256').update(bytes).digest('hex'),
  });
  await assert.rejects(prepareReportImport(new TextEncoder().encode('[]').buffer, '来源'), /INVALID_REPORT/);
  await assert.rejects(prepareReportImport(new ArrayBuffer(maxReportBytes + 1), '来源'), /REPORT_TOO_LARGE/);
});

test('identifies reused imports only when the submitted digest and previously observed ID match', () => {
  const observed = {id: 'previous-id', sha256: 'same-digest', origin: '首次来源'};
  assert.equal(reportImportNotice('same-digest', [observed], {...observed}),
    '已复用已有导入记录，首次登记的来源保持不变。');
  for (const [prior, returned] of [
    [[], observed],
    [[observed], {...observed, id: 'unobserved-id'}],
    [[{...observed, sha256: 'different-digest'}], observed],
    [[observed], {...observed, sha256: 'different-digest'}],
  ]) {
    assert.equal(reportImportNotice('same-digest', prior, returned), '导入记录已保存，等待核验。');
  }
});
