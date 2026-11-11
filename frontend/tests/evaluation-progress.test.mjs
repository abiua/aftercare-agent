import assert from 'node:assert/strict';
import test from 'node:test';
import { decodeProgress, evaluationUnsettled } from '../src/evaluationProgress.ts';

const experiment = 'fixture-progress';
const frame = (sequence, overrides = {}) => ({
  id: String(sequence), event: 'evaluation-progress',
  data: JSON.stringify({experiment_id: experiment, sequence, kind: 'RESULT_STATUS', ...overrides}),
});
const state = (latest_sequence, overrides = {}) => ({
  // The SSE reader inherits the previous event ID when the frame has no id field.
  id: String(latest_sequence), event: 'evaluation-state',
  data: JSON.stringify({experiment_id: experiment, latest_sequence, history: 'RECORDED', terminal: false, ...overrides}),
});

test('advances exactly one valid event and ignores a replayed sequence', () => {
  assert.deepEqual(decodeProgress(frame(21), experiment, 20), {kind: 'event', sequence: 21});
  assert.deepEqual(decodeProgress(frame(20), experiment, 20), {kind: 'ignore'});
  assert.deepEqual(decodeProgress(frame(22), experiment, 20), {kind: 'invalid'});
  assert.deepEqual(decodeProgress({...frame(21), id: '22'}, experiment, 20), {kind: 'invalid'});
  assert.deepEqual(decodeProgress(frame(21, {experiment_id: 'other'}), experiment, 20), {kind: 'invalid'});
});

test('accepts server state independent of inherited SSE ID and retains cancelled-but-unsettled work', () => {
  assert.deepEqual(decodeProgress(state(21, {status: 'CANCELLED'}), experiment, 21),
    {kind: 'state', terminal: false, history: 'RECORDED'});
  assert.deepEqual(decodeProgress(state(21, {terminal: true}), experiment, 21),
    {kind: 'state', terminal: true, history: 'RECORDED'});
  assert.deepEqual(decodeProgress(state(0, {history: 'HISTORICAL_SNAPSHOT'}), experiment, 0),
    {kind: 'state', terminal: false, history: 'HISTORICAL_SNAPSHOT'});
  assert.deepEqual(decodeProgress(state(22), experiment, 21), {kind: 'invalid'});
  assert.deepEqual(decodeProgress(state(21, {history: 'HISTORICAL_SNAPSHOT'}), experiment, 21), {kind: 'invalid'});
  assert.equal(evaluationUnsettled({status: 'CANCELLED', results: [{status: 'RUNNING'}]}), true);
  assert.equal(evaluationUnsettled({status: 'CANCELLED', results: [{status: 'NOT_RUN'}]}), false);
});
