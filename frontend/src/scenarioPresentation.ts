import type { ScenarioContributors, ScenarioScope, ScenarioSummary } from './contracts';

const units: Record<string, string> = {
  run: '次运行', check: '项检查', tool_decision: '次工具选择', tool_call: '次工具调用',
  citation: '条引文', fact: '项事实', policy: '项条款', event: '条事件',
  fault: '次故障', model_attempt: '次模型调用', logical_call: '次逻辑调用', route: '次路由',
  case: '个案例', load_operation: '次压力请求', interval: '段时间窗口', millisecond: '毫秒',
};

const reasons: Record<string, string> = {
  TASK_PASSED: '任务成功', TASK_NOT_PASSED: '任务未通过', RUN_NOT_STARTED: '运行未启动',
  RUN_FINISHED: '运行已结束', RUN_UNFINISHED: '运行尚未结束', OBSERVATION_PASSED: '检查通过',
  OBSERVATION_FAILED: '检查未通过', OBSERVATION_MISSING: '缺少观察记录', OBSERVATION_PRESENT: '已有观察记录',
  NOT_APPLICABLE: '本次运行不适用', AUDIT_MISSING: '缺少可核对的审计记录',
  COST_UNKNOWN: '费用尚不可知', COST_RECORDED: '已记录估算费用', NO_MODEL_CALLS: '本次运行未调用模型',
  PERCENTILE_SAMPLE: '计入耗时排序', DRAFT_BLOCKED: '程序拦截了草稿', NO_DRAFT_BLOCK: '未见草稿拦截记录',
  FINAL_VIOLATION: '记录到最终业务违规', NO_FINAL_VIOLATION: '已核对的结果没有最终违规',
  TOOL_REVIEW_PASS: '人工核对确认参数正确', TOOL_REVIEW_FAIL: '人工核对发现参数不正确',
  TOOL_REFERENCE_PASS: '程序比对确认参数符合参考', TOOL_REFERENCE_FAIL: '程序比对发现参数与参考不符',
  HUMAN_REVIEW_UNKNOWN: '复核结论为无法判断', CONFUSION_MATRIX_SAMPLE: '计入意图识别对照',
  REPETITION_GROUP: '计入同一案例的多次运行', LOAD_INTERVAL: '计入负载运行时间窗口',
  AUDIT_COMPLETE: '已有完整的审计记录', COST_DENOMINATOR: '计入费用指标的分母',
  HUMAN_REVIEW_PASS: '人工核对为正确', HUMAN_REVIEW_FAIL: '人工核对为不正确', HUMAN_REVIEW_MISSING: '缺少明确的人工判断',
};

export const contributionBuckets = {included: '计入统计', attention: '需关注', unknown: '未确定', excluded: '未计入'} as const;

export function scenarioUnit(unit: string) {
  return Object.hasOwn(units, unit) ? units[unit] : '个样本';
}

export function contributionReason(reason: string) {
  return Object.hasOwn(reasons, reason) ? reasons[reason] : '查看原始统计依据';
}

export function scenarioQuery(scope: ScenarioScope) {
  const query = new URLSearchParams();
  if (scope.scenario) query.set('scenario', scope.scenario);
  if (scope.intent) query.set('intent', scope.intent);
  return query;
}

function sameScope(first: ScenarioScope, second: ScenarioScope) {
  return first.scenario === second.scenario && first.intent === second.intent;
}

export function matchesScenarioSummary(report: ScenarioSummary, experiment: string, scope: ScenarioScope) {
  return report.experiment_id === experiment && sameScope(report.scope, scope);
}

export function matchesContributors(data: ScenarioContributors, report: ScenarioSummary, metricKey: string, offset: number, limit: number) {
  return data.experiment_id === report.experiment_id && sameScope(data.scope, report.scope)
    && data.basis_revision === report.basis_revision && data.metric.key === metricKey
    && data.offset === offset && data.limit === limit;
}
