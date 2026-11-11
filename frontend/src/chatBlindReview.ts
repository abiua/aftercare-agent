export type BlindStage = 'BLIND' | 'SEALED' | 'REVEALED' | 'LABELED';
export type BlindAction = 'REFUND' | 'REPLACE' | 'REQUEST_EVIDENCE' | 'NOT_SUPPORTED' | 'HUMAN_HANDOFF' | 'STATUS_ONLY';
export type BlindJudgment = 'DECIDABLE' | 'UNDETERMINED';
export type BlindLabel = 'ACCEPTABLE' | 'UNACCEPTABLE' | 'UNDETERMINED';
export type PolicyStatus = 'APPLICABLE' | 'NOT_APPLICABLE' | 'UNKNOWN';
export type BlindSummary = {id: string; case_label: string; input_sha256: string; status: BlindStage; version: number};
export type BlindPage = {items: BlindSummary[]; next_after: string | null};
export type BlindImage = {id: string; message_id: string; sha256: string; size: number; content_type: string};
type BlindFact = {id: string; kind: string; trust?: "VERIFIED_RECORD" | "UNVERIFIED_OBSERVATION"; data: Record<string, unknown>};
type BlindPolicy = {id: string; version: string; title: string; text: string; parameters: Record<string, unknown>; conditions: Record<string, unknown>};
export type BlindInput = {
  schema_version: 'chat-blind-input-v1' | 'chat-blind-input-v2'; intent: string;
  messages: {id: string; sequence: number; text: string}[];
  images: BlindImage[]; facts: BlindFact[]; policies: BlindPolicy[];
  policy_coverage: 'SCOPED_ONLY' | 'COMPLETE_CATALOGUE'; policy_context: Record<string, unknown> | null;
};
export type BlindLine = {line_id: string; amount_min_cents?: number; amount_max_cents?: number; quantity?: number};
export type BlindPlan = {action: BlindAction; lines: BlindLine[]; required_evidence: string[]};
export type InitialPayload = {
  judgment: BlindJudgment; acceptable_plans: BlindPlan[]; required_fact_ids: string[];
  policy_assessments: {policy_id: string; version: string; status: PolicyStatus}[]; notes: string;
};
type OverallLabel = {candidate_id: string; decision: BlindLabel; notes: string};
type OptionLabel = {candidate_id: string; option_id: string; decision: BlindLabel};
export type OptionLabelsPayload = {
  label_schema_version: 'chat-blind-labels-v2'; items: OverallLabel[]; option_labels: OptionLabel[];
};
export type LabelsPayload = {label_schema_version?: 'chat-blind-labels-v1'; items: OverallLabel[]} | OptionLabelsPayload;
export type BlindReference = {id: string; status: 'RESOLVED' | 'UNRESOLVED'; resolved_ids: string[]};
export type BlindCandidate = {id: string; status: 'AVAILABLE' | 'UNAVAILABLE'; unresolved: string[]; options: {
  id: string; action: BlindAction; lines: {line_id: string; amount_cents?: number | null; quantity?: number | null}[];
  required_evidence: string[]; rationale: string; alternatives: string[];
  evidence_ids: string[]; policy_ids: string[]; evidence_references: BlindReference[]; policy_references: BlindReference[];
}[]};
type Recorded<T> = {sha256: string; created_at: string; payload: T};
export type BlindDetail = BlindSummary & {
  input: BlindInput; initial: Recorded<InitialPayload> | null;
  exposure: {sha256: string; created_at: string; candidates: BlindCandidate[]} | null;
  labels: Recorded<LabelsPayload> | null;
};

export const blindStages: Record<BlindStage, string> = {
  BLIND: '待独立判断', SEALED: '初判已封存', REVEALED: '候选已揭示', LABELED: '后置复核已记录',
};
export const blindActions: Record<BlindAction, string> = {
  REFUND: '退款', REPLACE: '换发', REQUEST_EVIDENCE: '要求补证', NOT_SUPPORTED: '暂不支持办理',
  HUMAN_HANDOFF: '转人工核对', STATUS_ONLY: '仅查询进度',
};
export const blindLabels: Record<BlindLabel, string> = {
  ACCEPTABLE: '可以接受', UNACCEPTABLE: '不能接受', UNDETERMINED: '无法判断',
};
export const policyStatuses: Record<PolicyStatus, string> = {
  APPLICABLE: '适用', NOT_APPLICABLE: '不适用', UNKNOWN: '无法判断',
};

export function visibleBlindExposure(detail: BlindDetail) {
  return detail.initial && (detail.status === 'REVEALED' || detail.status === 'LABELED') ? detail.exposure : null;
}

export function latestBlindDetail(read: BlindDetail | undefined, written: BlindDetail | undefined) {
  if (written && (!read || (written.id === read.id && written.version > read.version))) return written;
  return read;
}
