import assert from 'node:assert/strict';
import {test} from 'node:test';
import {chatMessageBody, mergeChatMessages, mergeExecutionProgress, validChatFiles} from '../src/chat.ts';

test('conversation pages and replayed messages merge in sequence without duplicate display', () => {
  const first = {sequence: 1, id: 'first'};
  const last = {sequence: 3, id: 'last'};
  const second = {sequence: 2, id: 'second'};
  assert.deepEqual(mergeChatMessages([first, last], [last, second]), [first, second, last]);
});

test('message retries encode identical identifiers and ordered files without internal fields', () => {
  const files = [new File(['a'], 'first.png'), new File(['b'], 'second.png')];
  for (let attempt = 0; attempt < 2; attempt++) {
    const body = chatMessageBody('same-id', '补两张照片', files);
    assert.deepEqual([...body.keys()], ['client_message_id', 'text', 'files', 'files']);
    assert.equal(body.get('client_message_id'), 'same-id');
    assert.deepEqual(body.getAll('files').map((file) => file.name), ['first.png', 'second.png']);
  }
  assert.equal(validChatFiles(files), true);
  assert.equal(validChatFiles(Array(7).fill(files[0])), false);
  assert.equal(validChatFiles([new File([], 'empty.png')]), false);
  assert.equal(validChatFiles([{size: 8 * 1024 * 1024}, files[0]]), false);
});

test('a late response cannot revert analysis or revive an earlier request', async () => {
  const {analysisStatus, isCurrentEvidenceRequest, mergeChatAnalysis} = await import('../src/chat.ts');
  const base = {issue_id: 'old', generation: 3, state_version: 1, state: 'WAITING_UPLOADS', pending_uploads: 2};
  const old = {revision: 5, request: 1, value: base};
  const newer = {revision: 5, request: 2, value: {...base, state: 'COLLECTING', pending_uploads: 0}};
  assert.equal(mergeChatAnalysis(old, newer), newer);
  assert.equal(mergeChatAnalysis(newer, old), newer);
  const terminal = {revision: 5, request: 1, value: {...base, state: 'WAITING_CUSTOMER', state_version: 3}};
  assert.equal(mergeChatAnalysis(newer, terminal), terminal);
  assert.equal(isCurrentEvidenceRequest(terminal.value, base), true);
  const next = {revision: 6, request: 3, value: {...base, issue_id: 'new', generation: 4, state_version: 0}};
  assert.equal(mergeChatAnalysis(next, terminal), next);
  assert.equal(isCurrentEvidenceRequest(next.value, base), false);
  assert.equal(analysisStatus({...base, pending_uploads: 0}), '正在整理已收到的消息。');
  assert.equal(analysisStatus(base), '正在接收照片。');
  assert.equal(analysisStatus(undefined), '');
});

test('execution progress preserves omitted same-case mutation fields and rejects stale scope reads', () => {
  const input = (revision, active_case_id, execution_progress) => ({
    conversation: {revision, active_case_id, active_order_id: 'order'},
    features: {chat_decisions: true}, execution_progress,
  });
  const old = {id: 'old-execution', case_id: 'old-case'};
  const initial = mergeExecutionProgress(undefined, input(1, 'old-case', old), 2);
  const omitted = mergeExecutionProgress(initial, input(2, 'old-case', undefined), 3);
  assert.equal(omitted.value, old);
  const switched = mergeExecutionProgress(omitted, input(3, 'new-case', undefined), 4);
  assert.equal(switched.value, undefined);
  assert.equal(mergeExecutionProgress(switched, input(2, 'old-case', old), 5), switched);
  assert.equal(mergeExecutionProgress(switched, input(3, 'new-case', null), 6).value, null);
});
