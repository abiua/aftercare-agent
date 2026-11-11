import type { EvalResult } from './contracts';
import { materialLabels } from './casePresentation.ts';

type Data = Record<string, unknown>;
type TimelineAction = {id: string; position: number; summary: string; record: Data; snapshot?: Data};
export const reviewRecord = (value: unknown): Data | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : undefined;
const record = (value: unknown) => reviewRecord(value) ?? {};
const rows = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const text = (value: unknown) => typeof value === 'string' && value.trim() ? value : '';
const valid = (row: Data) => !row.detail && (typeof row.http_status !== 'number' || row.http_status < 400);
const lookup = (names: Record<string, string>, value: unknown) =>
  typeof value === 'string' && Object.hasOwn(names, value) ? names[value] : '';

const operationNames = {start: '助手开始处理', resume: '继续处理'};
const auditNames = {APPROVED: '客服审核照片通过', REJECTED: '客服审核照片未通过', NEEDS_MORE: '客服要求补充材料'};
const roleNames = {coordinator: '任务协调', policy: '政策查证', verifier: '结果核验', single: '售后助手'};

function scriptEntries(result: Pick<EvalResult, 'actual' | 'trace'>) {
  const events = record(record(result.actual).events);
  const script = rows(result.trace).filter((row) => text(row.event_id) && text(row.op));
  const counts = new Map<string, number>();
  for (const row of script) {
    const id = text(row.event_id);
    counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  const ordered = script.flatMap((trace, index) => {
    const id = text(trace.event_id);
    return counts.get(id) === 1 && Object.hasOwn(events, id)
      ? [{id, trace, result: record(events[id]), position: index + 1}] : [];
  });
  return {events, ordered};
}

export function reviewRunSteps(result: Pick<EvalResult, 'actual' | 'trace'>) {
  const {events, ordered} = scriptEntries(result);
  const seen = new Set(ordered.map(({id}) => id));
  return [
    ...ordered.filter(({trace}) => trace.op === 'start' || trace.op === 'resume'),
    ...Object.entries(events).flatMap(([id, row]) => {
      const item = record(row);
      return !seen.has(id) && (reviewRecord(item.output) || (text(item.thread_id) && item.output === null))
        ? [{id, trace: undefined, result: item, position: undefined}] : [];
    }),
  ];
}

export function readReviewEvidence(result: Pick<EvalResult, 'actual' | 'trace' | 'intent'>) {
  const {ordered} = scriptEntries(result);
  const questions = ordered.filter(({trace, result}) =>
    trace.op === 'create_case' && trace.actor === 'customer' && !trace.error && valid(result)
    && text(result.id) && text(result.order_id) && text(result.description));
  const customerQuestion = questions.length === 1 ? text(questions[0].result.description) : '';
  const intent = questions.length === 1 ? questions[0].result.intent ?? result.intent : result.intent;
  const actions = ordered.flatMap<TimelineAction>(({id, trace, result, position}) => {
    if (trace.error || !valid(result)) return [];
    if (trace.op === 'upload' && text(result.id)) {
      const photo = lookup(materialLabels, result.kind);
      return [{id, position, summary: photo ? `已上传${photo}` : '已上传材料', record: result}];
    }
    if (trace.op === 'review' && text(result.id)) {
      const summary = lookup(auditNames, result.decision);
      return summary ? [{id, position, summary, record: result}] : [];
    }
    if (trace.op === 'read' && reviewRecord(result.order) && reviewRecord(result.case)) {
      return [{id, position, summary: '已查询这时的工单与订单', record: result, snapshot: result}];
    }
    return [];
  });
  const runs = reviewRunSteps(result).filter(({trace, result: row}) =>
    trace || (text(row.id) && text(row.thread_id) && text(row.case_id))).map((step) => ({
    ...step,
    title: lookup(operationNames, step.trace?.op) || '处理记录（阶段顺序未确认）',
    state: step.result.output === null ? 'empty' as const :
      reviewRecord(step.result.output) ? 'recorded' as const : 'missing' as const,
    output: reviewRecord(step.result.output),
  }));
  const internalNotes = rows(result.trace).flatMap((event, index) => {
    const payload = record(event.payload);
    if (event.kind !== 'agent' || payload.stage !== 'finish' || !text(payload.notes)) return [];
    return [{index, role: lookup(roleNames, payload.role) || '助手角色', notes: text(payload.notes)}];
  });
  return {customerQuestion, intent, actions, runs, internalNotes, closeout: reviewRecord(record(result.actual).final_business)};
}
