import type { RunEvent } from './contracts';

export function mergeRunEvents(history: RunEvent[], incoming: RunEvent[]) {
  return [...new Map([...history, ...incoming].map((event) => [event.seq, event])).values()]
    .sort((left, right) => left.seq - right.seq);
}

const previewLimit = 4000;

export function provisionalDrafts(events: RunEvent[]) {
  const open = new Map<string, {key: string; alias: string; text: string; truncated: boolean}>();
  const discarded = new Set<string>();
  let active = true;
  for (const event of events) {
    const payload = event.payload;
    if (event.kind === 'status' && typeof payload.status === 'string') {
      active = payload.status === 'QUEUED' || payload.status === 'RUNNING';
      if (!active) open.clear();
    }
    if (!event.kind.startsWith('model_draft_')) continue;
    if (typeof payload.logical_id !== 'string' || typeof payload.ordinal !== 'number') continue;
    const key = JSON.stringify([payload.logical_id, payload.ordinal]);
    if (event.kind === 'model_draft_committed' || event.kind === 'model_draft_discarded') {
      discarded.add(key);
      open.delete(key);
    }
    if (!active || discarded.has(key) || event.kind !== 'model_draft_delta' || typeof payload.text !== 'string' || !payload.text) continue;
    const prior = open.get(key);
    const text = (prior?.text ?? '') + payload.text;
    open.set(key, {
      key, alias: typeof payload.alias === 'string' ? payload.alias : '',
      text: text.slice(0, previewLimit), truncated: Boolean(prior?.truncated || text.length > previewLimit),
    });
  }
  return [...open.values()];
}
