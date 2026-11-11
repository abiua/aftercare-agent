import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorMessage, type Client } from './api';
import { readSse, type SseFrame } from './sse';

export function useResource<T>(api: Client, path: string | null) {
  const [data, setData] = useState<{path: string; value: T}>();
  const [failure, setFailure] = useState<{path: string; message: string; code?: string}>();
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((value) => value + 1), []);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    setLoading(true);
    setFailure(undefined);
    api.get<T>(path, controller.signal).then((value) => {
      if (!controller.signal.aborted) setData({path, value});
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) {
        setFailure({path, message: errorMessage(failure), code: failure instanceof ApiError ? failure.code : undefined});
        if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) setData(undefined);
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [api, path, tick]);
  const currentFailure = path && failure?.path === path ? failure : undefined;
  return {value: path && data?.path === path ? data.value : undefined,
    error: currentFailure?.message ?? '', errorCode: currentFailure?.code, loading, refresh};
}

export function useActions(api: Client) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const active = useRef(false);
  const keys = useRef(new Map<string, string>());
  async function submit<T>(path: string, body: unknown, options: {success: string; fingerprint?: string; refresh?: () => void}) {
    if (active.current) return;
    active.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const fingerprint = `${path}\n${options.fingerprint ?? JSON.stringify(body)}`;
    const key = keys.current.get(fingerprint) ?? crypto.randomUUID();
    keys.current.set(fingerprint, key);
    try {
      const value = await api.post<T>(path, body, key);
      keys.current.delete(fingerprint);
      setNotice(options.success);
      options.refresh?.();
      return value;
    } catch (failure) {
      setError(failure instanceof ApiError && failure.code === 'NETWORK_ERROR'
        ? '服务暂时无法连接。当前表单保持打开且提交内容相同时，重试会沿用上一次请求编号。'
        : errorMessage(failure));
      if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500
        && failure.status !== 408 && failure.status !== 429) {
        keys.current.delete(fingerprint);
        if (failure.status === 409) options.refresh?.();
      }
    } finally {
      active.current = false;
      setBusy(false);
    }
  }
  return {submit, busy, error, notice};
}

export type StreamState = 'connecting' | 'connected' | 'reconnecting' | 'unavailable' | 'idle';

export function useStream(api: Client, path: string | null, onFrame: (frame: SseFrame) => void) {
  const [status, setStatus] = useState<StreamState>('idle');
  const listener = useRef(onFrame);
  useEffect(() => { listener.current = onFrame; }, [onFrame]);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    let cursor = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;
    const connect = async () => {
      try {
        const response = await api.stream(path, cursor, controller.signal);
        if (controller.signal.aborted) return;
        if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
          setStatus('unavailable');
          return;
        }
        setStatus('connected');
        retries = 0;
        let idle = false;
        await readSse(response.body, (frame) => {
          if (controller.signal.aborted) return;
          if (frame.event === 'idle') {
            idle = true;
            listener.current(frame);
            return;
          }
          const sequence = Number(frame.id);
          if (!Number.isSafeInteger(sequence) || sequence <= cursor || controller.signal.aborted) return;
          listener.current(frame);
          cursor = sequence;
        });
        if (idle && !controller.signal.aborted) {
          setStatus('idle');
          return;
        }
      } catch (error) {
        if (error instanceof ApiError && [401, 403, 404, 409, 501].includes(error.status)) {
          if (!controller.signal.aborted) setStatus('unavailable');
          return;
        }
      }
      if (!controller.signal.aborted) {
        setStatus('reconnecting');
        timer = setTimeout(connect, Math.min(1000 * 2 ** retries, 15000));
        retries += 1;
      }
    };
    setStatus('connecting');
    void connect();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, path]);
  return path ? status : 'idle';
}

export function useEventRefresh(refresh: () => void) {
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return useCallback(() => {
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = undefined;
      refresh();
    }, 150);
  }, [refresh]);
}
