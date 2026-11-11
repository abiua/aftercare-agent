import assert from 'node:assert/strict';
import {test} from 'node:test';
import {chatMessageBody, mergeChatMessages, validChatFiles} from '../src/chat.ts';

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
