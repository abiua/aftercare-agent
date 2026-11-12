import { knownEvaluationMessage } from './evaluationMessages';
import { intakeMessage } from './intake';

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

const messages: Record<string, string> = {
  LOGIN_FAILED: '账号或密码不正确。',
  NETWORK_ERROR: '服务暂时无法连接，请稍后重试。',
  INVALID_REFUND_EXECUTION_ID: '请输入一个完整的退款执行编号。',
  REFUND_LOOKUP_INTENT_REQUIRED: '请在退款进度工单中查询指定退款。',
  CHAT_MATERIAL_SCOPE_CHANGED: '当前售后事项已更新，请刷新后重新核对聊天照片。',
  BLIND_REVIEW_STAGE_CHANGED: '任务阶段已更新，请刷新后查看现有记录。',
  BLIND_REVIEW_CANDIDATES_UNAVAILABLE: '可揭示的候选尚未登记，请稍后再查看。',
  BLIND_REVIEW_EXPOSURE_REQUIRED: '请先完成候选揭示，再保存后置复核。',
  BLIND_REVIEW_REFERENCE_OUT_OF_SCOPE: '有事实、商品或条款不在本次冻结资料中。',
  BLIND_REVIEW_LABEL_COVERAGE_REQUIRED: '请给每一项已揭示候选选择一个结论。',
  BLIND_REVIEW_CANDIDATE_UNAVAILABLE: '没有可供核对的候选只能选择「无法判断」。',
  DECISION_INPUT_REQUIRED: '请先让客户确认订单和商品，并关联当前售后事项。',
  DECISION_STALE: '本次售后信息已更新，请重新核对候选方案。',
  DECISION_REVIEW_STALE: '客服复核记录已更新，请查看当前版本。',
  DECISION_ALREADY_REVIEWED: '该候选已有复核记录，请查看当前版本。',
  DECISION_UNRESOLVED: '方案还有未核对的事项。',
  DECISION_OPTION_UNAVAILABLE: '此候选方案仍需核对，暂时无法采纳。',
  DECISION_OFFER_STALE: '客服方案已更新或失效，请刷新查看。',
  DECISION_OFFER_ALREADY_ANSWERED: '这份方案已有回复，请刷新查看。',
  DECISION_CUSTOMER_CONSENT_REQUIRED: '请核对客户已接受的方案，再继续办理。',
  DECISION_RESOLUTION_UNKNOWN: '待核对事项已更新，请重新查看候选方案。',
  DECISION_LINES_OUT_OF_SCOPE: '方案包含当前售后范围之外的商品。',
  DECISION_EVIDENCE_GAP_REQUIRED: '请勾选需要客户补充的内容。',
  DECISION_EVIDENCE_OUT_OF_SCOPE: '方案包含当前售后事项无法使用的材料。',
  DECISION_POLICY_OUT_OF_SCOPE: '方案依据需要重新核对。',
  DECISION_BINDING_INVALID: '当前订单或售后事项需要重新确认。',
  INVALID_RESPONSE: '服务返回了无法读取的数据，请刷新后查看当前状态。',
};

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    const known = knownEvaluationMessage(error.code) ?? intakeMessage(error.code);
    if (known) return known;
    if (Object.hasOwn(messages, error.code)) return messages[error.code];
    if (error.status === 401) return '登录凭据失效，请重新登录。';
    if (error.status === 403) return '当前身份没有执行此操作的权限。';
    if (error.status === 404) return '记录不存在，或当前身份无权查看。';
    if (error.status === 409) return `当前记录有冲突。请核对页面上的最新状态。 ${error.message}`;
    if (error.status === 413) return '文件超过上传限制。';
    return messages[error.code] ?? error.message;
  }
  return '操作未完成，请重试。';
}

function apiUrl(path: string) { return `/api${path}`; }

async function response(token: string, path: string, init?: RequestInit) {
  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${token}`);
  let res;
  try {
    res = await fetch(apiUrl(path), {...init, headers, cache: 'no-store'});
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    throw new ApiError(0, 'NETWORK_ERROR', '连接失败');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(res.status, body?.detail?.code ?? '', body?.detail?.message ?? '请求未完成，请核对输入。');
  }
  return res;
}

async function json<T>(token: string, path: string, init?: RequestInit) {
  const res = await response(token, path, init);
  try {
    return await res.json() as T;
  } catch {
    throw new ApiError(0, 'INVALID_RESPONSE', '响应无法读取');
  }
}

export function client(token: string) {
  return {
    get: <T>(path: string, signal?: AbortSignal) => json<T>(token, path, {signal}),
    post: <T>(path: string, body: unknown, key?: string) => {
      const headers = new Headers();
      if (key) headers.set('Idempotency-Key', key);
      const multipart = body instanceof FormData;
      if (!multipart) headers.set('Content-Type', 'application/json');
      return json<T>(token, path, {method: 'POST', body: multipart ? body : JSON.stringify(body), headers});
    },
    blob: async (path: string, signal?: AbortSignal) => (await response(token, path, {signal})).blob(),
    stream: (path: string, cursor: number, signal: AbortSignal) => {
      const headers = new Headers({'Accept': 'text/event-stream'});
      if (cursor) headers.set('Last-Event-ID', String(cursor));
      const separator = path.includes('?') ? '&' : '?';
      return response(token, `${path}${separator}after=${cursor}`, {headers, signal});
    },
  };
}
export type Client = ReturnType<typeof client>;
export const segment = (id: string) => encodeURIComponent(id);
