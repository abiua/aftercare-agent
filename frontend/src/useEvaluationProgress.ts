import { useEffect, useState } from 'react';
import { ApiError, type Client } from './api';
import { decodeProgress } from './evaluationProgress';
import { readSse } from './sse';

export type EvaluationProgressStatus = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'polling' | 'finished' | 'denied';

const reconnectDelays = [500, 1000, 2000, 4000];
class InvalidProgress extends Error {}

export function useEvaluationProgress(api: Client, path: string | null, experimentId: string,
  onProgress: () => void, onSettled: () => void, onDenied: () => void): EvaluationProgressStatus {
  const [status, setStatus] = useState<EvaluationProgressStatus>('idle');
  useEffect(() => {
    if (!path) return;
    const session = new AbortController();
    let cursor = 0;
    let retries = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const stop = (status: EvaluationProgressStatus) => {
      setStatus(status);
      session.abort();
    };

    async function connect(progressPath: string) {
      try {
        if (retries > 0) {
          // Check authorization and continuity before reopening a connection that ended without a final state.
          // This page is a probe. Only accepted stream events advance the cursor.
          await api.get(`${progressPath}?after=${cursor}&limit=1`, session.signal);
        }
        if (session.signal.aborted) return;
        const response = await api.stream(`${progressPath}/stream`, cursor, session.signal);
        if (session.signal.aborted) return;
        if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
          stop('polling');
          return;
        }
        await readSse(response.body, (frame) => {
          if (session.signal.aborted) return;
          const update = decodeProgress(frame, experimentId, cursor);
          if (update.kind === 'invalid') throw new InvalidProgress();
          if (update.kind === 'event') {
            cursor = update.sequence;
            setStatus('live');
            onProgress();
          }
          if (update.kind === 'state') {
            if (update.terminal) {
              stop('finished');
              onSettled();
            } else if (update.history === 'HISTORICAL_SNAPSHOT') {
              stop('polling');
              onProgress();
            } else {
              setStatus('live');
            }
          }
        });
      } catch (error) {
        if (session.signal.aborted) return;
        if (error instanceof ApiError && [401, 403].includes(error.status)) {
          stop('denied');
          onDenied();
          return;
        }
        if (error instanceof InvalidProgress || (error instanceof ApiError && [400, 404, 409, 501].includes(error.status))) {
          stop('polling');
          return;
        }
      }
      if (session.signal.aborted) return;
      if (retries >= reconnectDelays.length) {
        stop('polling');
        return;
      }
      setStatus('reconnecting');
      retryTimer = setTimeout(() => void connect(progressPath), reconnectDelays[retries++]);
    }

    setStatus('connecting');
    void connect(path);
    return () => {
      session.abort();
      clearTimeout(retryTimer);
    };
  }, [api, path, experimentId, onProgress, onSettled, onDenied]);
  return path ? status : 'idle';
}
