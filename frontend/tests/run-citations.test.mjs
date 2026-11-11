import assert from 'node:assert/strict';
import { test } from 'node:test';
import { describeRunCitations } from '../src/runCitations.ts';

const existing = {id: 'POL08', version: '1.0', quote: '已有处理记录'};
const unresolved = {id: 'POL12', version: '1.0', quote: '结果未知时核对'};
const assessment = (citation, condition_status) => ({
  ...citation, condition_status, use: 'RECONCILE_UNKNOWN_REFUND',
  reason_code: 'FIXTURE_REASON', registry_version: 'test-v1',
  scope: {kind: 'ORDER_REFUND_EXECUTION_LEDGER', execution_ids: ['fixture-execution']},
});

test('groups record-supported quotations separately from unmet and unknown conditions', () => {
  const unknown = {...unresolved, quote: '核对另一段引文'};
  const recorded = assessment(unresolved, 'MET');
  const view = describeRunCitations({
    citations: [existing, unresolved, unknown],
    citation_assessments: [assessment(existing, 'UNMET'), recorded, assessment(unknown, 'UNKNOWN')],
  });
  assert.deepEqual(view.supported, [{
    citation: unresolved, assessment: recorded, status: 'MET', label: '前提有记录支持',
  }]);
  assert.deepEqual(view.background.map(({citation, label}) => [citation, label]), [
    [existing, '条件未成立'], [unknown, '证据不足'],
  ]);
});

test('joins only exact ID, version and quote and keeps legacy and unregistered quotations as background', () => {
  const other = {id: 'POL01', version: '1.0', quote: '另一条政策'};
  for (const citation_assessments of [undefined, [
    assessment({...existing, id: 'POL09'}, 'MET'),
    assessment({...existing, version: '2.0'}, 'MET'),
    assessment({...existing, quote: existing.quote + '。'}, 'MET'),
  ]]) {
    const view = describeRunCitations({citations: [existing], citation_assessments});
    assert.deepEqual(view.supported, []);
    assert.deepEqual(view.background, [{
      citation: existing, assessment: undefined, status: 'NOT_ASSESSED', label: '条件未评估',
    }]);
  }
  const unregistered = assessment(other, 'NOT_ASSESSED');
  assert.deepEqual(describeRunCitations({citations: [other], citation_assessments: [unregistered]}).background, [{
    citation: other, assessment: unregistered, status: 'NOT_ASSESSED', label: '条件未评估',
  }]);
});

test('does not treat candidate policy IDs or assessment-only rows as citations', () => {
  assert.deepEqual(describeRunCitations({
    policy_ids: ['POL08', 'POL12'], citation_assessments: [assessment(existing, 'MET')],
  }), {supported: [], background: []});
});
