import { knownEvaluationMessage } from './evaluationMessages';
import { intakeMessage } from './intake';

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

const messages: Record<string, string> = {
  NETWORK_ERROR: '服务暂时无法连接，请稍后重试。',
  INVALID_REFUND_EXECUTION_ID: '请输入一个完整的退款执行编号。',
  REFUND_LOOKUP_INTENT_REQUIRED: '请在退款进度工单中查询指定退款。',
  INVALID_RESPONSE: '服务返回了无法读取的数据，请刷新后查看当前状态。',
};

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    const known = knownEvaluationMessage(error.code) ?? intakeMessage(error.code);
    if (known) return known;
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
