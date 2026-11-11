import type { EvalResult, EvaluationCapabilities, ReleaseThresholds } from './contracts';
import { materialLabels } from './casePresentation.ts';
import { recoveryReason } from './evaluationMessages.ts';

const expectedSteps: Record<string, string> = {
  WAITING_INPUT: '先等待客户补充材料', MATERIAL_REVIEW_REQUIRED: '材料齐全后等待人工审核',
  ELIGIBLE: '核对资格后可拟定方案', STATUS_ONLY: '根据账本查询退款进度',
  NEEDS_REVIEW: '交由人工核对', HUMAN_REVIEW: '交由人工核对',
  INELIGIBLE: '按规则说明暂不能办理',
};
const actualSteps: Record<string, string> = {
  WAITING_INPUT: '已请求补充材料', MATERIAL_REVIEW_REQUIRED: '等待人工材料审核',
  ELIGIBLE: '已核对处理资格', STATUS_ONLY: '已按现有记录查询退款进度',
  NEEDS_REVIEW: '已转待人工复核', HUMAN_REVIEW: '等待人工核对',
  INELIGIBLE: '未满足处理条件', FAILED: '助手运行失败', TIMEOUT: '助手运行超时',
  COMPLETED: '助手步骤已完成',
};
const actions: Record<string, string> = {REFUND: '退款', REPLACE: '换货', CANCEL: '取消订单'};
const evidenceSources: Record<string, string> = {
  live_provider: '真实模型调用记录', application_offline: '离线规则运行', fault_in_process: '故障注入运行',
  no_provider_call: '模型调用未发生',
};
const checkNames: Record<string, string> = {
  status: '运行状态', reason: '处理原因', 'output.action': '方案动作',
  'output.missing_materials': '待补材料', 'output.historical_refunded_cents': '历史退款金额',
  'output.ledger': '退款执行账本', 'output.citations': '政策原文引用', 'output.policy_ids': '参考条款编号',
  no_unauthorized_execution: '执行权限', ledger_count: '账本记录数量', 'http.status': '请求结果',
};

const record = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const str = (value: unknown) => typeof value === 'string' ? value : '';
const finite = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
const lookup = (values: Record<string, string>, value: unknown) => {
  const key = str(value);
  return Object.hasOwn(values, key) ? values[key] : '';
};
const unique = (values: string[]) => [...new Set(values.filter(Boolean))];
const yuan = (value: number) => `¥${(value / 100).toFixed(2)}`;

function amountRange(range: unknown) {
  const lower = finite(record(range).min);
  const upper = finite(record(range).max);
  if (lower === undefined || upper === undefined || lower > upper || lower < 0) return '';
  return lower === upper ? yuan(lower) : `${yuan(lower)} 至 ${yuan(upper)}`;
}

function moneyValue(value: unknown) {
  if (typeof value !== 'number' && (typeof value !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value))) return;
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? amount : undefined;
}

export function summarizeEvaluation(result: Pick<EvalResult, 'expected' | 'actual' | 'checks' | 'trace'>) {
  const expected = record(result.expected);
  const actual = record(result.actual);
  const references = array(expected.checks).map(record);
  const expectedActions = unique(array(expected.allowed_draft_actions).map((action) => lookup(actions, action)));
  const expectedMaterials = unique(references.flatMap((check) => check.path === 'output.missing_materials'
    ? array(check.equals).map((item) => lookup(materialLabels, item)) : []));
  const expectedPath = unique(references.filter((check) => check.path === 'reason' || check.path === 'status')
    .map((check) => lookup(expectedSteps, check.equals)));
  const timeline = Object.values(record(actual.events)).map(record);
  const traces = array(result.trace).map((event) => record(record(event).payload));
  const actualPath = unique((timeline.length ? timeline : traces).flatMap((event) => {
    const output = record(event.output);
    const action = lookup(actions, output.action);
    const step = recoveryReason(str(event.reason)) || lookup(actualSteps, event.reason) || (event.output ? lookup(actualSteps, event.status) : '');
    const materials = unique(array(output.missing_materials).map((item) => lookup(materialLabels, item)));
    return [
      step ? [step, materials.length ? materials.join('、') : ''].filter(Boolean).join('：') : '',
      action ? `已拟定${action}草案` : '',
    ];
  }));
  const citations = new Map<string, {id: string; version: string; title: string; quote: string}>();
  for (const step of [...timeline, ...traces]) {
    for (const item of array(record(step.output).citations).map(record)) {
      const id = str(item.id), version = str(item.version);
      const quote = str(item.quote);
      if (!id || !version || !quote) continue;
      citations.set(`${id}:${version}`, {id, version, title: str(item.title) || id, quote});
    }
  }
  const auditComplete = actual.model_audit_complete === true && Array.isArray(actual.model_attempts);
  const attempts = array(actual.model_attempts).map(record);
  const amounts = attempts.map((attempt) => moneyValue(attempt.estimated_cost_usd));
  const estimate = auditComplete && amounts.every((amount) => amount !== undefined)
    ? amounts.reduce<number>((sum, amount) => sum + (amount ?? 0), 0) : undefined;
  const checks = array(result.checks).map(record);
  const keyChecks = checks.filter((check) => check.status === 'FAIL' || check.status === 'UNKNOWN').map((check) => ({
    status: str(check.status), label: lookup(checkNames, check.path) || '其他业务检查', event: str(check.event_id),
  }));
  return {
    expectedPath, expectedActions, expectedMaterials, expectedAmount: amountRange(expected.refund_amount_cents),
    referencePolicies: unique(array(expected.required_policy_ids).map(str)),
    actualPath,
    citations: [...citations.values()],
    calls: auditComplete || attempts.length ? attempts.length : undefined,
    estimate, auditComplete, models: unique(attempts.map((attempt) => str(attempt.model))),
    source: lookup(evidenceSources, actual.observed_source) || '证据来源待核对',
    passed: checks.filter((check) => check.status === 'PASS').length,
    failed: checks.filter((check) => check.status === 'FAIL').length,
    unknown: checks.filter((check) => check.status === 'UNKNOWN').length,
    checkCount: checks.length, keyChecks,
  };
}

export function describeThresholds(thresholds: ReleaseThresholds | undefined) {
  if (!thresholds) return [];
  const percentage = (value: number) => value.toLocaleString('zh-CN', {style: 'percent', maximumFractionDigits: 4});
  const definitions: [keyof ReleaseThresholds, string, (value: number) => string][] = [
    ['min_coverage', '最低执行覆盖率', percentage],
    ['min_task_success', '最低任务成功率', percentage],
    ['min_independent_cases', '最少独立案例数', (value) => `${value} 个`],
    ['min_human_gold', '最少独立人工核验数', (value) => `${value} 个`],
    ['max_total_cost_usd', '估算总费用上限', (value) => `$${value}`],
    ['max_cost_per_success', '每个成功案例的估算费用上限', (value) => `$${value}`],
    ['max_final_violation_rate', '最终违规率上限', percentage],
  ];
  const items = definitions.flatMap(([key, label, format]) => {
    const value = moneyValue(thresholds[key]);
    return value === undefined ? [] : [{label, value: format(value)}];
  });
  if (thresholds.require_live === true) items.push({label: '评测来源', value: '需要真实模型证据'});
  return items;
}

export function evaluationFeatures(capabilities?: EvaluationCapabilities, runtime?: Record<string, boolean>) {
  const supported = capabilities ?? runtime;
  return {
    importEnabled: supported?.eval_import === true,
    retryEnabled: supported?.eval_retry === true,
    gatesEnabled: supported?.eval_gates === true,
  };
}
