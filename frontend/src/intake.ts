import type { IntakeClassifier, IntakeIntent, IntakePreview, IntakePreviewRequest } from './contracts';

export const intakeIntents: IntakeIntent[] = ['damaged', 'wrong_item', 'refund_status'];

const reasons: Record<string, string> = {
  ORDER_REQUIRED: '请选择需要售后的订单。',
  NO_ORDERS: '当前账号暂无可选择的订单。',
  MULTIPLE_ORDERS: '有多笔本人订单，请选择要办理售后的那一笔。',
  ORDER_NOT_AVAILABLE: '描述中的订单暂不可用，请从本人订单中选择。',
  INTENT_REQUIRED: '请选择希望办理的售后类型。',
  INTAKE_CUSTOMER_REQUIRED: '请使用本人客户账号发起售后。',
};

export function intakeMessage(code?: string | null) {
  return code && Object.hasOwn(reasons, code) ? reasons[code] : undefined;
}

const sources: Record<IntakePreview['classifier_source'], string> = {
  rules: '按规则识别',
  confirmed: '根据你的选择',
  model: '由模型识别',
  model_unavailable: '模型暂不可用，请手动选择',
};

export function intakeSource(source: IntakePreview['classifier_source']) {
  return Object.hasOwn(sources, source) ? sources[source] : '识别来源待核对';
}

export type IntakeDraft = {message: string; orderId?: string; intent?: IntakeIntent};
export type BoundIntakePreview = {inputKey: string; result: IntakePreview};

export function intakePreviewRequest(draft: IntakeDraft, classifier: IntakeClassifier = 'rules'): IntakePreviewRequest {
  return {message: draft.message.trim(), order_id: draft.orderId, intent: draft.intent, classifier};
}

export function intakeInputKey(draft: IntakeDraft) {
  return JSON.stringify([draft.message.trim(), draft.orderId || null, draft.intent || null]);
}

export function intakeConfirmRequest(draft: IntakeDraft, preview?: BoundIntakePreview) {
  if (!preview || preview.inputKey !== intakeInputKey(draft)) return;
  const result = preview.result;
  if (
    result.status !== 'READY' || result.required.length || !result.order?.id || !result.intent
    || !intakeIntents.includes(result.intent) || !Number.isSafeInteger(result.order.version) || result.order.version < 1
    || (draft.orderId && draft.orderId !== result.order.id) || (draft.intent && draft.intent !== result.intent)
  ) return;
  return {
    message: draft.message.trim(),
    order_id: result.order.id,
    intent: result.intent,
    expected_version: result.order.version,
    confirmed: true as const,
  };
}
