import type { RefundLookupExecution, RefundLookupResponse, RefundLookupResult } from './contracts';

const executionIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const statusText: Record<string, string> = {
  SUCCEEDED: '模拟退款处理成功',
  FAILED: '模拟退款处理失败',
  SUBMITTING: '模拟退款正在提交',
  UNKNOWN: '模拟退款结果待核对',
};

const outcomeText: Record<Exclude<RefundLookupResult['status'], 'FOUND'>, string> = {
  REFERENCE_REQUIRED: '要查某一笔退款，请提供退款执行编号。',
  AMBIGUOUS_REFERENCE: '请先确认要查询哪一笔退款，每次只能查一个执行编号。',
  INVALID_REFERENCE: '请输入一个完整的退款执行编号。',
  NOT_FOUND: '这份工单的订单中未找到该笔退款执行记录。请核对编号。',
};

export function refundExecutionId(input: string) {
  const id = input.trim();
  return executionIdPattern.test(id) ? id.toLowerCase() : undefined;
}

function isRefundExecution(value: unknown): value is RefundLookupExecution {
  if (typeof value !== 'object' || !value) return false;
  const row = value as Partial<RefundLookupExecution>;
  return typeof row.id === 'string' && refundExecutionId(row.id) === row.id
    && typeof row.status === 'string' && Number.isSafeInteger(row.amount_cents)
    && typeof row.created_at === 'string' && typeof row.receipt_confirmed === 'boolean';
}

export function matchingRefundExecution(response: RefundLookupResponse, id: string) {
  const execution = response?.execution;
  return response?.schema_version === 'ac-v1' && response.status === 'FOUND'
    && isRefundExecution(execution) && execution.id === id ? execution : undefined;
}

export function readRefundLookup(value: unknown): RefundLookupResult | undefined {
  if (!value || typeof value !== 'object' || !('status' in value) || !('execution' in value)) return;
  if (value.status === 'FOUND' && isRefundExecution(value.execution)) {
    return {status: 'FOUND', execution: value.execution};
  }
  if (typeof value.status === 'string' && Object.hasOwn(outcomeText, value.status) && value.execution === null) {
    return {status: value.status as keyof typeof outcomeText, execution: null};
  }
}

export function describeRefundExecution(execution: RefundLookupExecution) {
  const {status} = execution;
  return {
    status: Object.hasOwn(statusText, status) ? statusText[status] : statusText.UNKNOWN,
    receipt: execution.receipt_confirmed && (status === 'SUCCEEDED' || status === 'FAILED')
      ? '本地模拟回执已确认' : '未确认模拟回执',
  };
}

export function refundLookupMessage(status: Exclude<RefundLookupResult['status'], 'FOUND'>) {
  return outcomeText[status];
}
