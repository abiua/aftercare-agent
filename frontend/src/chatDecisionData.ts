import type {Order} from './contracts';

export type DecisionRef = {id: string; version: number; sha256: string};
export type DecisionLine = {line_id: string; amount_cents?: number | null; quantity?: number | null};
type OptionBase = {id: string; rationale: string; evidence_ids: string[]};
export type ExecutionOption = OptionBase & {action: 'REFUND' | 'REPLACE'; lines: DecisionLine[]; policy_ids: string[]};
export type NonExecutionOption = OptionBase & {action: 'REQUEST_EVIDENCE' | 'NOT_SUPPORTED' | 'HUMAN_HANDOFF';
  target_line_ids: string[]; missing_evidence: string[]; alternatives: string[]};
export type DecisionOption = ExecutionOption | NonExecutionOption;
export type ProgramCheck = {status: 'PASSED' | 'BLOCKED' | 'NON_EXECUTING'; code?: string; policy_ids?: string[]};
export type DecisionSnapshot = DecisionRef & {
  origin: 'AGENT' | 'SUPPORT_MODIFICATION'; parent_id: string | null; original_run_id: string;
  input: {case_id: string; order_id: string; line_ids: string[]; input_through_seq: number;
    evidence_ids: string[]; input_refs: {kind: string; id: string; sha256: string}[]; business_versions: Record<string, string | number>};
  candidate: {options: DecisionOption[]; unresolved: string[]};
  checks: Record<string, ProgramCheck>; expires_at: string;
};
export type ReviewAction = 'ACCEPT' | 'MODIFY' | 'REJECT' | 'REQUEST_EVIDENCE' | 'TAKE_OVER';
export const evidenceGaps = {
  ITEM: '商品实物照片', PARCEL: '包裹外观照片', WRONG_ITEM: '错发商品实物照片',
  LABEL: '包裹标签照片', ISSUE_DESCRIPTION: '补充问题描述',
} as const;
export type EvidenceGap = keyof typeof evidenceGaps;
export type CustomerAction = 'ACCEPT' | 'REJECT' | 'REQUEST_CHANGE';
export type SupportDecisionView = {issue_id: string | null; snapshot: DecisionSnapshot | null; expected_review_id: string | null;
  customer_decision?: {id: string; action: CustomerAction; reason?: string | null; created_at: string; offer: DecisionRef} | null;
  latest_review: {id: string; action: ReviewAction; reason: string; created_at: string; program_check?: ProgramCheck | null} | null};
export type CustomerOffer = DecisionRef & {
  action: ExecutionOption['action']; lines: DecisionLine[]; policy_ids: string[]; expires_at: string;
  customer_decision: {id: string; action: CustomerAction} | null;
};
export type CustomerOfferView = {issue_id: string | null; offer: CustomerOffer | null};

export const decisionActions: Record<DecisionOption['action'], string> = {
  REFUND: '退款', REPLACE: '换发', REQUEST_EVIDENCE: '补充材料', NOT_SUPPORTED: '暂不支持办理', HUMAN_HANDOFF: '转人工处理',
};
export const reviewActions: Record<ReviewAction, string> = {
  ACCEPT: '采纳', MODIFY: '修改', REJECT: '拒绝', REQUEST_EVIDENCE: '要求补证', TAKE_OVER: '接管',
};
export const customerActions: Record<CustomerAction, string> = {ACCEPT: '接受方案', REJECT: '拒绝方案', REQUEST_CHANGE: '提出修改诉求'};

export function decisionRef({id, version, sha256}: DecisionRef): DecisionRef { return {id, version, sha256}; }
export function isExecutionOption(option: DecisionOption): option is ExecutionOption {
  return option.action === 'REFUND' || option.action === 'REPLACE';
}
export function canAcceptOption(snapshot: DecisionSnapshot, option: DecisionOption) {
  return isExecutionOption(option) ? snapshot.checks[option.id]?.status === 'PASSED' && !snapshot.candidate.unresolved.length :
    snapshot.checks[option.id]?.status === 'NON_EXECUTING';
}
export function orderLineName(order: Order | undefined, id: string) {
  const line = order?.items.find((item) => item.line_id === id);
  return order?.products.find((product) => product.id === line?.product_id)?.name ?? `商品行 ${id}`;
}
export function editableMeasure(value: string, action: ExecutionOption['action']) {
  if (action === 'REPLACE') return /^\d+$/.test(value) && Number(value) > 0 && Number(value) <= 2_147_483_647 ? Number(value) : undefined;
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return undefined;
  const [whole, fraction = ''] = value.split('.');
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  return cents > 0 && cents <= 2_147_483_647 ? cents : undefined;
}
