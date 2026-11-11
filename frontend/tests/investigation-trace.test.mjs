import assert from 'node:assert/strict';
import {test} from 'node:test';
import {investigationTrace, isInvestigationEvent} from '../src/investigationTraceData.ts';

const row = (kind, payload, seq = 1) => ({seq, kind: `investigation_${kind}`, payload, created_at: '2026-11-11T00:00:00Z'});

test('investigation rendering data allowlists references, excluding frozen inputs, tool arguments and routing output', () => {
  const [route, tool] = investigationTrace([
    row('route', {path: 'SINGLE', selected_path: 'MULTI', output: {customer_message: 'private-original'},
      financial_risk: {maximum_refundable_cents: 0, has_unsettled_refund: false},
      evidence_refs: [{source_id: 'fact-id', original: 'private-original'}]}),
    row('tool', {role: 'fact', tool: 'lookup', evidence_ids: ['fact-id', 42], arguments: {private: 'private-original'}}),
  ]);
  assert.equal(route.amount, 0);
  assert.equal(route.unsettled, false);
  assert.deepEqual(route.refs, ['fact-id']);
  assert.deepEqual(tool.refs, ['fact-id']);
  assert.equal(JSON.stringify([route, tool]).includes('private-original'), false);
});

test('unknown investigation kinds are still classified as internal; invalid claims add no invented evidence', () => {
  const unknown = row('future', {raw: 'private'});
  assert.equal(isInvestigationEvent(unknown), true);
  assert.deepEqual(investigationTrace([unknown]), []);
  const [result] = investigationTrace([row('result', {claims: [null, {}, {id: 'c1', state: 'UNRESOLVED'}]})]);
  assert.deepEqual(result.claims.map(({id, state, supporting}) => ({id, state, supporting})), [
    {id: 'c1', state: 'UNRESOLVED', supporting: []},
  ]);
});

test('recheck differences require recorded before and after assessments and retain unknowns', () => {
  const source = [
    {id: 'fact/revised', value: '旧判断'}, {id: 'fact/kept', value: '原值'},
    {id: 'policy/stale', value: '依赖旧判断'},
  ];
  const final = [{...source[0], value: '新判断'}, source[1]];
  const after = row('assessment', {recheck_count: 1, stale_claim_ids: ['policy/stale'], claims: final}, 3);
  const [alone] = investigationTrace([after]);
  assert.equal('kept' in alone, false);
  const [, , comparison] = investigationTrace([
    row('assessment', {recheck_count: 0, claims: source}),
    row('recheck', {claim_ids: ['fact/revised'], stale_claim_ids: ['fact/revised', 'policy/stale']}, 2),
    after,
  ]);
  assert.deepEqual(comparison.kept, ['fact/kept']);
  assert.deepEqual(comparison.revised, ['fact/revised']);
  assert.deepEqual(comparison.stale, ['policy/stale']);
});
