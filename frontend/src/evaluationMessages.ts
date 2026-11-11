const recoveryReasons: Record<string, string> = {
  RECOVERY_CONTEXT_CHANGED: '原运行依赖的证据或配置已变化。请人工核对后，再发起新一轮运行。',
  RECOVERY_CONTEXT_UNBOUND: '原运行缺少可核对的恢复记录。请人工确认后，再发起新一轮运行。',
  REPLAY_VALIDATION_FAILED: '原运行保留的调用结果无法按当前规则核验。请人工核对后，再发起新一轮运行。',
};

export function recoveryReason(code?: string | null) {
  return code && Object.hasOwn(recoveryReasons, code) ? recoveryReasons[code] : undefined;
}

const reasons: Record<string, string> = {
  ...recoveryReasons,
  HUMAN_REFERENCE_PENDING: '案例参考待人工核验',
  INSUFFICIENT_EVIDENCE: '现有记录不足以判断结果',
  NO_PROVIDER_CALL: '本次未记录到真实模型调用',
  NO_MODEL_CANDIDATE: '当前没有可用模型',
  FROZEN_INPUT_CHANGED: '冻结的输入或配置已变化，本次未执行',
  WORKER_INTERRUPTED: '运行进程中断，已有证据已保留',
  EVALUATION_ACTOR_REVOKED: '测试员权限已变更，后续案例未启动',
  EXPERIMENT_CANCELLED: '实验已取消',
  DETERMINISTIC_CHECK_FAILED: '有业务规则检查未通过',
  LIVE_BUDGET_UNALLOCATED: '本案未分配到足够的调用或费用预算',
  FAULT_SCRIPT_UNAVAILABLE: '该案例没有适用的故障脚本',
  FAULT_SOURCE_REQUIRED: '该案例需要使用故障注入方式运行',
  MODEL_BUDGET_EXHAUSTED: '已达到模型调用上限',
  MODEL_COST_BUDGET_EXHAUSTED: '已达到模型费用上限',
  MODEL_RATE_LIMIT: '模型服务限制了请求频率',
  MODEL_TIMEOUT: '等待模型响应超时',
  MODEL_OUTPUT_INVALID: '模型输出无法核验',
  TOOL_TIMEOUT: '等待工具响应超时',
  EXECUTION_TIMEOUT: '案例执行超时',
  TASK_TIMEOUT: '案例执行超时',
  TIMEOUT: '案例执行超时',
  RESULT_NOT_RETRYABLE: '所选案例当前不能重跑，请核对原实验结果。',
  RETRY_SELECTION_INVALID: '请从原实验选择可以重跑的案例。',
  RETRY_CONFIG_CHANGED: '原实验的冻结条件已变化，无法按原条件重跑。',
  METRIC_BASIS_CHANGED: '指标使用的运行或复核记录已更新，请查看最新统计。',
  VERSION_CONFLICT: '记录已有更新，请刷新后重新确认。',
  EVALUATION_REQUIRES_TEST: '请在独立测试环境办理评测操作。',
  EVALUATION_FORBIDDEN: '评测工作台仅供评测人员使用。',
  CODE_RESTART_REQUIRED: '评测服务的代码已更新，请在服务重启后再试。',
  INVALID_REPORT: '报告无法导入，请选择符合格式的评测 JSON 文件。',
  REPORT_HASH_MISMATCH: '报告内容校验未通过，请重新选择原始文件。',
  REPORT_TOO_LARGE: '评测报告超过 8 MB。',
  IMPORT_NOT_FOUND: '未找到这份导入报告。',
  GATE_POLICY_NOT_FOUND: '所选发布阈值已无法读取，请重新选择。',
  CASE_SELECTION_EMPTY: '当前划分没有可运行的案例。',
  INVALID_EXPERIMENT: '请核对实验名称和运行设置。',
  EXPERIMENT_FINISHED: '实验已经结束，请查看最新结果。',
  INVALID_REVIEW: '请选择复核结论并填写依据。',
  RESULT_NOT_FINISHED: '请在案例结束后再复核。',
};

export const knownEvaluationMessage = (code: string) => Object.hasOwn(reasons, code) ? reasons[code] : undefined;

export function evaluationReason(code?: string | null) {
  if (!code) return '';
  if (knownEvaluationMessage(code)) return knownEvaluationMessage(code);
  return /[\u4e00-\u9fff]/u.test(code) ? code : '此结果需要进一步核对，详情见原始证据。';
}
