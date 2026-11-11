import type { CaseDetail, Execution } from './contracts';

export const materialLabels: Record<string, string> = {
  ITEM: '商品照片', PARCEL: '包裹照片', WRONG_ITEM: '错发商品实物照片', LABEL: '包裹标签照片',
};

export function receiptId(receipt: unknown) {
  if (typeof receipt !== 'object' || receipt === null || !('id' in receipt)) return;
  return typeof receipt.id === 'string' && receipt.id.trim() ? receipt.id : undefined;
}

export function confirmedRefundReceiptId({status, receipt}: Execution) {
  return status === 'SUCCEEDED' || status === 'FAILED' ? receiptId(receipt) : undefined;
}

export function canActBeforeExecution({case: current, ledger}: Pick<CaseDetail, 'case' | 'ledger'>, proposalId: string) {
  if (['EXECUTION_PENDING', 'EXECUTION_FAILED', 'RESOLVED'].includes(current.status)) return false;
  // Non-status-query detail only includes ledger entries from the current case.
  return !ledger.some((row) => row.proposal_id === proposalId || row.case_id === current.id
    || (!row.case_id && current.intent !== 'refund_status'));
}

export function orderRefundPresentation({ledger, order}: Pick<CaseDetail, 'ledger' | 'order'>, readFailed: boolean) {
  const rows = readFailed ? [] : ledger.filter((row) => row.action === 'REFUND');
  const historicalRefundedCents = !readFailed && order.items.length
    ? order.items.reduce((total, line) => total + line.refunded_cents, 0) : undefined;
  return {rows, historicalRefundedCents, notice: refundLedgerNotice(rows, readFailed)};
}

function refundLedgerNotice(refunds: Execution[], readFailed: boolean) {
  if (readFailed) return {
    type: 'warning' as const, title: '暂时无法更新退款执行记录',
    description: '请刷新工单后再核对该订单的退款执行账本。',
  };
  if (!refunds.length) return {
    type: 'info' as const, title: '未查到该订单的退款执行记录',
    description: '是否曾申请、是否处理失败或是否实际到账，需另行核对。',
  };
  const unknown = refunds.filter(({status}) => status === 'UNKNOWN').length;
  const submitting = refunds.filter(({status}) => status === 'SUBMITTING').length;
  const unconfirmed = refunds.length - unknown - submitting - refunds.filter(confirmedRefundReceiptId).length;
  const pending = [
    unknown ? `${unknown} 笔结果未知` : '',
    submitting ? `${submitting} 笔正在提交` : '',
    unconfirmed ? `${unconfirmed} 笔缺少可确认的模拟回执` : '',
  ].filter(Boolean);
  if (pending.length) return {
    type: 'warning' as const, title: '已有退款执行记录，结果待核对',
    description: `该订单共有 ${refunds.length} 笔退款执行记录，其中${pending.join('、')}。`,
  };
  return {
    type: 'info' as const, title: '已查到该订单的退款执行记录',
    description: '请查看下方每笔记录的模拟执行结果和回执。实际到账需另行核对。',
  };
}
