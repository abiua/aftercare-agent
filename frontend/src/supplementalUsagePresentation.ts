import type { ProviderTokenUsage, SupplementalUsageReport } from './contracts';

const unavailableReasons: Record<NonNullable<SupplementalUsageReport['unavailable_reason']>, string> = {
  RESULT_NOT_FINISHED: '请在案例结束后再核对补充记录。',
  SNAPSHOT_FORMAT_UNSUPPORTED: '原报告缺少可供配对的用量快照。',
  SCHEMA_NOT_FOUND: '本次运行的用量账本已无法读取。',
  AUDIT_UNREADABLE: '当前无法读取本次运行的用量账本。',
  AUDIT_STORAGE_UNSUPPORTED: '当前存储方式无法读取补充用量。',
};

export function supplementalUsageMessage(report: SupplementalUsageReport) {
  if (report.status === 'UNAVAILABLE') return {
    title: '补充用量审计证据不可用',
    detail: report.unavailable_reason ? unavailableReasons[report.unavailable_reason] : undefined,
  };
  const unmatched = [
    report.unmatched_attempt_count ? `当前账本有 ${report.unmatched_attempt_count} 条调用记录无法与原快照配对。` : '',
    report.unmatched_snapshot_attempt_count ? `原快照有 ${report.unmatched_snapshot_attempt_count} 条调用记录无法与当前账本配对。` : '',
  ].filter(Boolean).join('');
  if (report.attempts.length) return {
    title: `发现 ${report.attempts.length} 次已配对调用的记录差异`,
    detail: unmatched || undefined,
  };
  return {
    title: unmatched ? '存在无法配对的调用记录' : '未发现可配对调用的记录差异',
    detail: unmatched || undefined,
  };
}

export function formatSupplementalCost(cost: string | null) {
  return cost == null ? '未知' : `$${cost}`;
}

export function formatProviderTokens(usage: ProviderTokenUsage | null) {
  const counters: [string, number | undefined][] = [
    ['输入', usage?.input_tokens],
    ['输出', usage?.output_tokens],
    ['总量', usage?.total_tokens],
    ['缓存读取', usage?.input_token_details?.cache_read],
    ['缓存写入', usage?.input_token_details?.cache_creation],
    ['输入音频', usage?.input_token_details?.audio],
    ['推理', usage?.output_token_details?.reasoning],
    ['输出音频', usage?.output_token_details?.audio],
  ];
  return counters.flatMap(([label, count]) =>
    typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
      ? [`${label} ${count.toLocaleString('zh-CN')}`] : []);
}
