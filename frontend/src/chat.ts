import type {Order} from './contracts';

export type Conversation = {
  id: string; customer_id: string; status: 'OPEN' | 'HUMAN_HANDOFF' | 'CLOSED';
  revision: number; created_at: string; updated_at: string;
  active_case_id: string | null; active_order_id: string | null;
};
export type ChatAttachment = {id: string; filename: string; content_type: string; size: number; sha256: string; content_path: string};
export type OrderCandidate = Pick<Order, 'id' | 'items' | 'status' | 'version'> & Partial<Pick<Order, 'products'>>;
export type ChatCard =
  | {type: 'policy_consultation'; data: {retrieval: {source_url?: string; version?: string; status?: string}; excerpts: {text: string; title?: string; policy_id?: string; version?: string; effective_at?: string; line?: number; chunk_id: string}[]}}
  | {type: 'order_candidates'; data: {orders: OrderCandidate[]; new_issue: boolean}}
  | {type: 'case_link'; data: {case_id: string; order_id: string}}
  | {type: 'status'; data: {code: string; refunds?: {id: string; status: string; amount_cents: number; receipt_confirmed?: boolean}[]}}
  | {type: 'photo_observation'; data: ChatAnalysisKey & {attachment_refs: string[]; summary: string; certainty: 'OBSERVED' | 'UNCERTAIN' | 'CONTRADICTED'}}
  | {type: 'evidence_request'; data: ChatAnalysisKey & {request_id: string; prompt: string; alternatives: string[]}};
type ChatAnalysisKey = {issue_id: string; generation: number};
export type ChatAnalysis = ChatAnalysisKey & {
  state_version: number;
  state: 'COLLECTING' | 'WAITING_UPLOADS' | 'CHECKING_PHOTOS' | 'WAITING_CUSTOMER' | 'HUMAN_HANDOFF' | 'COMPLETE' | 'UNAVAILABLE';
  input_through_seq: number; pending_uploads: number; wait_until: string | null;
};
export type ChatMessage = {id: string; conversation_id: string; sequence: number; role: 'customer' | 'support' | 'assistant';
  text: string; created_at: string; attachments: ChatAttachment[]; cards: ChatCard[]};
export type ChatExecutionProgress = {
  id: string; case_id: string; action: 'REFUND' | 'REPLACE';
  status: 'SUBMITTING' | 'UNKNOWN' | 'SUCCEEDED' | 'FAILED';
  amount_cents: number; lines: {line_id: string; amount_cents?: number; quantity?: number}[];
  simulated: boolean; receipt_confirmed: boolean; updated_at: string;
};
export type ChatInvestigationProgress = {state: 'QUEUED' | 'CHECKING' | 'PENDING_SUPPORT' | 'NEEDS_SUPPORT'};
type CustomerStatement = {source: 'CUSTOMER_STATEMENT'; message_id: string; sequence: number; text: string; sha256: string};
export type ChatMemory = {
  schema_version: 1; issue_id: string; conversation_revision: number;
  state: 'CURRENT' | 'CORRECTION_NEEDS_SUPPORT' | 'PENDING_SCOPE_CONFIRMATION';
  confirmed_context: {case_id: string | null; order_id: string | null; line_ids: string[]};
  latest_request: CustomerStatement | null; correction: CustomerStatement | null;
  recent_attachments: {attachment_id: string; message_id: string; sequence: number; sha256: string; filename: string; current: boolean}[];
  has_more_attachments: boolean;
};
export type ChatUpdate = {conversation: Conversation; messages: ChatMessage[]; memory?: ChatMemory; analysis?: ChatAnalysis; features?: {chat_decisions?: boolean; chat_material_bridge?: boolean}; execution_progress?: ChatExecutionProgress | null; investigation_progress?: ChatInvestigationProgress | null};
export type ChatPage = ChatUpdate & {has_more: boolean; next_after: number};

export const CHAT_PAGE_SIZE = 50;

type ExecutionRead = {revision: number; request: number; scope: string; value?: ChatExecutionProgress | null};
export function mergeExecutionProgress(previous: ExecutionRead | undefined, update: ChatUpdate, request: number): ExecutionRead {
  const {revision, active_case_id, active_order_id} = update.conversation;
  const scope = JSON.stringify([active_case_id, active_order_id]);
  const incoming = {revision, request, scope, value: update.execution_progress};
  if (!previous) return incoming;
  if (revision < previous.revision || (revision === previous.revision && request < previous.request)) return previous;
  if (scope === previous.scope && update.features?.chat_decisions === true && update.execution_progress === undefined) {
    return {...previous, revision};
  }
  return incoming;
}

type InvestigationRead = {revision: number; request: number; value?: ChatInvestigationProgress | null};
export function mergeInvestigationProgress(previous: InvestigationRead | undefined, update: ChatUpdate, request: number): InvestigationRead {
  const {revision} = update.conversation;
  if (previous && (revision < previous.revision || (revision === previous.revision && request < previous.request))) return previous;
  if (previous && revision === previous.revision && update.investigation_progress === undefined) return previous;
  return {revision, request, value: update.investigation_progress};
}

export function investigationStatus(progress: ChatInvestigationProgress | null | undefined, analysis: ChatAnalysis | undefined) {
  switch (progress?.state) {
    case 'QUEUED': return analysis?.state === 'WAITING_CUSTOMER' ? '' : '已排队核对售后事项。';
    case 'CHECKING': return analysis?.state === 'WAITING_CUSTOMER' ? '' : '正在核对售后事项。';
    case 'PENDING_SUPPORT': return '已整理好调查结果，等待客服核对。';
    case 'NEEDS_SUPPORT': return '当前无法完成自动核对，请联系人工客服。';
    default: return '';
  }
}

type AnalysisRead = {revision: number; request: number; value?: ChatAnalysis};
export function mergeChatAnalysis(previous: AnalysisRead | undefined, incoming: AnalysisRead): AnalysisRead {
  if (!previous || incoming.revision > previous.revision) return incoming;
  if (incoming.revision < previous.revision) return previous;
  if (!incoming.value) return previous.value ? previous : incoming;
  if (!previous.value) return incoming;
  const order = incoming.value.generation - previous.value.generation ||
    incoming.value.state_version - previous.value.state_version || incoming.request - previous.request;
  return order >= 0 ? incoming : previous;
}

export function isCurrentEvidenceRequest(analysis: ChatAnalysis | undefined, card: ChatAnalysisKey) {
  return analysis?.state === 'WAITING_CUSTOMER' && analysis.issue_id === card.issue_id &&
    analysis.generation === card.generation;
}

export function analysisStatus(analysis: ChatAnalysis | undefined) {
  switch (analysis?.state) {
    case 'COLLECTING': return '正在整理已收到的消息。你可以继续补充。';
    case 'WAITING_UPLOADS': return analysis.pending_uploads > 0 ? '正在接收照片。' : '正在整理已收到的消息。';
    case 'CHECKING_PHOTOS': return '正在检查已收到的信息。';
    case 'WAITING_CUSTOMER': return '等待补充信息。';
    case 'HUMAN_HANDOFF': return '已转客服处理。';
    case 'UNAVAILABLE': return '当前无法自动检查。你可以转客服处理。';
    default: return '';
  }
}

export function mergeChatMessages(current: ChatMessage[], incoming: ChatMessage[]) {
  const merged = new Map(current.map((row) => [row.sequence, row]));
  for (const row of incoming) merged.set(row.sequence, row);
  return [...merged.values()].sort((a, b) => a.sequence - b.sequence);
}

export function chatMessageBody(id: string, text: string, files: File[]) {
  const body = new FormData();
  body.append('client_message_id', id);
  body.append('text', text);
  for (const file of files) body.append('files', file);
  return body;
}

export function validChatFiles(files: File[]) {
  return files.length <= 6 && files.every((file) => file.size > 0) &&
    files.reduce((total, file) => total + file.size, 0) <= 8 * 1024 * 1024;
}
