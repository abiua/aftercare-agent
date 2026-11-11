import type {Order} from './contracts';

export type Conversation = {
  id: string; customer_id: string; status: 'OPEN' | 'HUMAN_HANDOFF' | 'CLOSED';
  revision: number; created_at: string; updated_at: string;
  active_case_id: string | null; active_order_id: string | null;
};
export type ChatAttachment = {id: string; filename: string; content_type: string; size: number; sha256: string; content_path: string};
export type OrderCandidate = Pick<Order, 'id' | 'items' | 'status' | 'version'> & Partial<Pick<Order, 'products'>>;
export type ChatCard =
  | {type: 'order_candidates'; data: {orders: OrderCandidate[]; new_issue: boolean}}
  | {type: 'case_link'; data: {case_id: string; order_id: string}}
  | {type: 'status'; data: {code: string; refunds?: {id: string; status: string; amount_cents: number; receipt_confirmed?: boolean}[]}};
export type ChatMessage = {id: string; conversation_id: string; sequence: number; role: 'customer' | 'support' | 'assistant';
  text: string; created_at: string; attachments: ChatAttachment[]; cards: ChatCard[]};
export type ChatUpdate = {conversation: Conversation; messages: ChatMessage[]};
export type ChatPage = ChatUpdate & {has_more: boolean; next_after: number};

export const CHAT_PAGE_SIZE = 50;
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
