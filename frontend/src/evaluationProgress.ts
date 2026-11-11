import type { Experiment } from './contracts';
import type { SseFrame } from './sse';

export function evaluationUnsettled(experiment?: Pick<Experiment, 'status' | 'results'>) {
  return !!experiment && (pending(experiment.status) || experiment.results.some((row) => pending(row.machine_status ?? row.status)));
}

function pending(status: string) {
  return status === 'QUEUED' || status === 'RUNNING';
}

type ProgressUpdate =
  | {kind: 'event'; sequence: number}
  | {kind: 'state'; history: 'RECORDED' | 'HISTORICAL_SNAPSHOT'; terminal: boolean}
  | {kind: 'ignore' | 'invalid'};

export function decodeProgress(frame: SseFrame, experimentId: string, cursor: number): ProgressUpdate {
  if (frame.event !== 'evaluation-progress' && frame.event !== 'evaluation-state') return {kind: 'ignore'};
  let row: unknown;
  try {
    row = JSON.parse(frame.data);
  } catch {
    return {kind: 'invalid'};
  }
  if (!row || typeof row !== 'object' || !('experiment_id' in row) || row.experiment_id !== experimentId) {
    return {kind: 'invalid'};
  }
  if (frame.event === 'evaluation-state') {
    if (!('history' in row) || (row.history !== 'RECORDED' && row.history !== 'HISTORICAL_SNAPSHOT')
        || !('terminal' in row) || typeof row.terminal !== 'boolean'
        || !('latest_sequence' in row) || row.latest_sequence !== cursor
        || (row.history === 'HISTORICAL_SNAPSHOT' && cursor !== 0)) return {kind: 'invalid'};
    return {kind: 'state', history: row.history, terminal: row.terminal};
  }
  if (!('sequence' in row) || typeof row.sequence !== 'number' || !Number.isSafeInteger(row.sequence)
      || row.sequence < 1 || frame.id !== String(row.sequence)
      || !('kind' in row) || (row.kind !== 'EXPERIMENT_STATUS' && row.kind !== 'RESULT_STATUS')) return {kind: 'invalid'};
  if (row.sequence <= cursor) return {kind: 'ignore'};
  return row.sequence === cursor + 1 ? {kind: 'event', sequence: row.sequence} : {kind: 'invalid'};
}
