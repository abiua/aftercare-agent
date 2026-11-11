import { Alert, Button, Collapse, Empty, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ReactNode } from 'react';
import type { Intent, Role } from './contracts';
import type { StreamState } from './hooks';
import { materialLabels } from './casePresentation';

export const roles: Record<Role, string> = {customer: '客户', support: '售后客服', approver: '审批人', tester: '评测人员'};
export const intents: Record<Intent, string> = {
  damaged: '商品破损', wrong_item: '收到错发商品', refund_status: '查询退款进度',
  cancel: '取消订单', return: '退货申请', warranty: '保修申请',
};
const labels: Record<string, string> = {
  OPEN: '待处理', WAITING_INPUT: '等待补充材料', HUMAN_REVIEW: '待人工审核', ELIGIBLE: '符合处理条件',
  INELIGIBLE: '暂不符合处理条件', STATUS_ONLY: '仅查询进度', PROPOSED: '待审批', APPROVED: '已通过',
  REJECTED: '未通过', NEEDS_MORE: '需补充材料', EXECUTION_PENDING: '处理中', RESOLVED: '已办结',
  EXECUTED: '已执行', SUPERSEDED: '已被新版本替代', PAID: '已付款', SHIPPED: '已交运', DELIVERED: '已签收',
  UNSHIPPED: '未交运', UNVERIFIED: '待核对', CONFIRMED: '已确认', NOT_RECEIVED: '未收到', RECEIVED: '已收到', NOT_STARTED: '未开始',
  EXECUTION_FAILED: '执行失败', SUBMITTING: '正在提交', UNKNOWN: '未知，待确认', SUCCEEDED: '已完成',
  FAILED: '失败', QUEUED: '排队中', RUNNING: '运行中', NEEDS_REVIEW: '待人工复核', COMPLETED: '已完成',
  CANCELED: '已取消', CANCELLED: '已取消', PASS: '通过', PASSED: '通过', FAIL: '失败', TIMEOUT: '超时',
  ERROR: '执行错误', NOT_RUN: '未运行', INCOMPLETE: '未完成', DRAFT: '草案', STALE: '已失效',
  EXPIRED: '已过期', INVALIDATED: '已失效', PENDING: '等待中', REFUND: '退款', REPLACE: '换货',
  ...materialLabels,
  fixture: '受控测试', live: '真实模型评测', fault: '故障注入', load: '压力测试',
  economy: '经济模型', analysis: '分析模型', routed: '按规则路由', multi: '三 Agent 协作', single: '单 Agent',
  regression: '冲突回归集', dev: '开发集', validation: '验证集', test: '保留集', holdout: '保留集',
  easy: '基础', medium: '中等', hard: '复杂', conflict: '冲突回归',
  UNPAIRED: '未配对', REGRESSION: '新增失败', IMPROVED: '已修复', FIXED: '已修复', CHANGED: '结果变化', UNCHANGED: '无变化',
  PENDING_INDEPENDENT_APPROVAL: '待独立审批', BLOCKED: '暂不可发布', imported_unverified: '导入记录 · 待核验',
};
export const label = (value: string | null | undefined) => value ? labels[value] ?? value : '未知';
const colors: Record<string, string> = {
  APPROVED: 'green', ELIGIBLE: 'green', RESOLVED: 'green', SUCCEEDED: 'green', COMPLETED: 'green', PASS: 'green',
  PASSED: 'green', FAILED: 'red', FAIL: 'red', REJECTED: 'red', ERROR: 'red', EXECUTION_FAILED: 'red',
  TIMEOUT: 'red', UNPAIRED: 'gold', UNKNOWN: 'gold', NEEDS_REVIEW: 'gold', WAITING_INPUT: 'gold', NEEDS_MORE: 'gold',
  HUMAN_REVIEW: 'gold', RUNNING: 'blue', PROPOSED: 'blue', QUEUED: 'blue',
};
export function Status({value}: {value: string | null | undefined}) {
  return <Tag color={value ? colors[value] : undefined}>{label(value)}</Tag>;
}

export function StreamBadge({state}: {state: StreamState}) {
  const labels: Record<StreamState, string> = {
    connecting: '连接中', connected: '实时更新', reconnecting: '正在重连', unavailable: '实时更新不可用', idle: '实时更新未开启',
  };
  return <Tag color={state === 'connected' ? 'green' : state === 'reconnecting' ? 'gold' : 'default'}>{labels[state]}</Tag>;
}

export function Feedback({error, notice}: {error?: string; notice?: string}) {
  return <>
    {error && <Alert type="error" showIcon title={error} />}
    {notice && <Alert type="success" showIcon title={notice} />}
  </>;
}
export function Reload({refresh, loading}: {refresh: () => void; loading?: boolean}) {
  return <Button icon={<ReloadOutlined />} onClick={refresh} loading={loading}>刷新</Button>;
}
export function Placeholder({text}: {text: string}) {
  return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={text} />;
}
export const money = (cents: number | undefined | null) => cents == null ? '未知' : `¥${(cents / 100).toFixed(2)}`;
export function time(value: string | undefined | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {hour12: false});
}
export function Evidence({title = '展开查看证据', children}: {title?: string; children: ReactNode}) {
  return <Collapse size="small" ghost items={[{key: 'evidence', label: title, children}]} />;
}
export function Json({value}: {value: unknown}) {
  return value == null ? <span className="muted">暂无记录</span> : <pre className="json">{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>;
}
