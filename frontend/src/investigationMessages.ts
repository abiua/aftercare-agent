const investigationReasons: Record<string, string> = {
  HUMAN_REVIEW: '调查已完成，待客服审核具体建议；财务审批另行办理。',
  MODEL_TIMEOUT: '本轮模型响应超时，尚未形成完整方案。请由客服核对已有调查记录。',
  MODEL_INCOMPLETE: '模型未完成本轮输出，尚未形成完整方案。请由客服继续核对。',
  INVESTIGATION_PROMPT_LIMIT: '本轮需要核对的内容超过一次调查可处理的范围，调查已停止。请由客服查看已有记录。',
  INVESTIGATION_INPUT_TOKEN_LIMIT: '本轮输入超过调查可处理的范围。请由客服核对现有材料。',
  INVESTIGATION_MODEL_UNAVAILABLE: '当前没有可用于本轮调查的模型。请由客服继续核对。',
  MODEL_UNAVAILABLE: '当前没有可用于本轮调查的模型。请由客服继续核对。',
  INVESTIGATION_PROMPT_VERSION_CHANGED: '调查配置版本已更新。请核对本案资料后，再发起新一轮调查。',
  INVESTIGATION_DEPENDENCIES_CHANGED: '调查依赖的订单、材料或规则已有更新。请先核对当前记录。',
  INVESTIGATION_INPUT_STALE: '调查开始后，本案资料已有更新。请查看当前记录。',
  INVESTIGATION_INPUT_SUPERSEDED: '本轮所用资料已被更新。请以当前事项的记录为准。',
  INVESTIGATION_SCOPE_CHANGED: '本次售后事项的范围已变化。请核对当前订单和商品。',
  INVESTIGATION_EVIDENCE_INCONSISTENT: '调查使用的证据记录不一致。请核对原材料和业务记录。',
  INVESTIGATION_MATERIAL_READ_FAILED: '已有材料读取失败，请客服检查原图和审核记录。',
  INVESTIGATION_MATERIAL_NOT_READ: '已有照片或审核记录尚未核对，请客服查看这些记录，无需让客户重复上传。',
  INVESTIGATION_EVIDENCE_MISSING: '现有证据不足以完成调查。请核对缺少的材料或业务记录。',
  INVESTIGATION_DEPENDENT_CLAIMS_UNRESOLVED: '复查后仍有依赖原判断的事项待核对。请由客服查看这些判断。',
  INVESTIGATION_RECHECK_UNRESOLVED: '复查后仍有分歧未解决。请由客服继续核对。',
  INVESTIGATION_ISSUE_CALL_LIMIT: '本次售后事项已达到自动调查次数上限。请由客服接手核对。',
  INVESTIGATION_ISSUE_COST_LIMIT: '本次售后事项的调查费用预算已用尽。请由客服接手核对。',
  INVESTIGATION_ISSUE_COST_UNKNOWN: '前次调查费用尚无法确认，后续自动调查已暂停。',
  INVESTIGATION_ISSUE_BUDGET_VIOLATION: '本次售后事项的调查费用超出预算，后续自动调查已暂停。',
  INVESTIGATION_ESTIMATE_EXCEEDED: '预计本轮调用会超过预留额度，已停止继续调用。',
  INVESTIGATION_SPEND_UNCERTAIN: '已有调用的用量或费用尚待核对，后续自动调查已暂停。',
  MODEL_BUDGET_EXHAUSTED: '本轮调查已达到模型调用次数上限。',
  MODEL_COST_BUDGET_EXHAUSTED: '本轮调查已达到模型费用上限。',
};

export function investigationReason(code?: string | null) {
  return code && Object.hasOwn(investigationReasons, code) ? investigationReasons[code] : undefined;
}
