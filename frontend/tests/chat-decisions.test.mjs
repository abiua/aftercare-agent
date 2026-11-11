import assert from 'node:assert/strict';
import {test} from 'node:test';
import {canAcceptOption, decisionRef, editableMeasure} from '../src/chatDecisionData.ts';

test('only checked, resolved executable options and checked non-executing options can be adopted', () => {
  const refund = {id: 'refund', action: 'REFUND'};
  const evidence = {id: 'evidence', action: 'REQUEST_EVIDENCE'};
  const snapshot = {candidate: {unresolved: []}, checks: {refund: {status: 'PASSED'}, evidence: {status: 'NON_EXECUTING'}}};
  assert.equal(canAcceptOption(snapshot, refund), true);
  snapshot.candidate.unresolved.push('验收状态');
  assert.equal(canAcceptOption(snapshot, refund), false);
  assert.equal(canAcceptOption(snapshot, evidence), true);
  assert.equal(canAcceptOption(snapshot, {id: 'unknown', action: 'REFUND'}), false);
});

test('decision ref keeps only immutable version identity, and changes preserve integer units', () => {
  assert.deepEqual(decisionRef({id: 'offer', version: 2, sha256: 'digest', action: 'REFUND'}),
    {id: 'offer', version: 2, sha256: 'digest'});
  assert.equal(editableMeasure('8.25', 'REFUND'), 825);
  assert.equal(editableMeasure('21474836.47', 'REFUND'), 2147483647);
  assert.equal(editableMeasure('21474836.48', 'REFUND'), undefined);
  assert.equal(editableMeasure('0.001', 'REFUND'), undefined);
  assert.equal(editableMeasure('2', 'REPLACE'), 2);
  assert.equal(editableMeasure('2.5', 'REPLACE'), undefined);
});
