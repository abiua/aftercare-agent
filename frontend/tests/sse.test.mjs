import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readSse } from '../src/sse.ts';

function stream(bytes, slice = 1) {
  return new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += slice) controller.enqueue(bytes.slice(i, i + slice));
      controller.close();
    },
  });
}

test('restores split UTF-8, CRLF, and multi-line data across chunks', async () => {
  const input = ': heartbeat\r\nid: 13\r\nevent: business\r\ndata: 第一行\r\ndata: 第二行\r\n\r\n';
  const frames = [];
  await readSse(stream(new TextEncoder().encode(input)), (frame) => frames.push(frame));
  assert.deepEqual(frames, [{id: '13', event: 'business', data: '第一行\n第二行'}]);
});

test('supports heartbeat and explicit idle without inventing an event ID', async () => {
  const input = 'retry: 1500\n\nid: 8\ndata: {"seq":8}\n\n: heartbeat\n\nevent: idle\ndata: {"status":"WAITING_INPUT"}\n\n';
  const frames = [];
  await readSse(stream(new TextEncoder().encode(input), 5), (frame) => frames.push(frame));
  assert.deepEqual(frames, [
    {id: '8', event: '', data: '{"seq":8}'},
    {id: '8', event: 'idle', data: '{"status":"WAITING_INPUT"}'},
  ]);
});

test('ignores an incomplete final frame and preserves the latest valid ID', async () => {
  const input = 'id: 2\rdata: first\r\rid: 3\0\rdata: second\r\rid: 4\rdata: unfinished\r';
  const frames = [];
  await readSse(stream(new TextEncoder().encode(input), 7), (frame) => frames.push(frame));
  assert.deepEqual(frames, [
    {id: '2', event: '', data: 'first'},
    {id: '2', event: '', data: 'second'},
  ]);
});

test('reconnect replay does not duplicate draft text or resurrect a discarded attempt', async () => {
  const { mergeRunEvents, provisionalDrafts } = await import('../src/runDrafts.ts');
  const draft = (seq, kind, ordinal, text) => ({seq, kind, payload: {logical_id: 'call', ordinal, alias: 'economy', text}});
  const prior = [
    draft(1, 'model_draft_delta', 1, '片段一'),
    draft(2, 'model_draft_delta', 1, '片段二'),
  ];
  const replay = [
    prior[1], draft(3, 'model_draft_discarded', 1), draft(4, 'model_draft_delta', 1, '迟到片段'),
    draft(5, 'model_draft_delta', 2, '新的草稿'),
  ];
  const frames = [];
  const text = replay.map((event) => `id: ${event.seq}\ndata: ${JSON.stringify(event)}\n\n`).join('');
  await readSse(stream(new TextEncoder().encode(text), 3), (frame) => frames.push(JSON.parse(frame.data)));
  const beforeDiscard = mergeRunEvents(prior, [frames[0]]);
  assert.equal(provisionalDrafts(beforeDiscard)[0].text, '片段一片段二');
  const merged = mergeRunEvents(prior, frames);
  assert.deepEqual(provisionalDrafts(merged).map((item) => item.text), ['新的草稿']);
  assert.deepEqual(provisionalDrafts([...merged, draft(6, 'model_draft_committed', 2)]), []);
  assert.deepEqual(provisionalDrafts([...merged, {seq: 6, kind: 'status', payload: {status: 'COMPLETED'}}]), []);
});
