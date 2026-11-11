import assert from 'node:assert/strict';
import test from 'node:test';
import { buildReviewAnnotations, recordedAnnotations, reviewFacts, reviewSections } from '../src/reviewAnnotations.ts';

test('review keys use ordered tool calls, exact audited citations and required fact strings only', () => {
  const input = {
    trace: [
      {kind: 'tool_validation', payload: {seq: 13}},
      {kind: 'tool', seq: 14, payload: {call_id: 'c1', arguments: {}}},
      {kind: 'tool', seq: 19, payload: {call_id: 'c2'}},
    ],
    expected: {
      required_facts: ['material_review', 'payment', '__proto__', 'constructor', 'payment'],
      tool_decisions: [{required_arguments: {}}],
    },
    actual: {
      citation_audit: [{key: 'POL06@1.0', applicable: true}, {key: 'POL06@1.0', applicable: true}],
      events: {
        e01: {output: {notes: '早期说明', citations: [{id: 'POL06', version: '1.0', quote: '引文一'}],
          citation_assessments: [
            {id: 'POL06', version: '1.0', quote: '引文一', condition_status: 'UNMET'},
            {id: 'POL06', version: '1.0', quote: '引文二', condition_status: 'MET'},
          ]}},
        e04: {output: {notes: '后来说明', citations: [
          {id: 'POL06', version: '2.0', quote: '其他版本'}, {id: 'POL06', version: '1.0', quote: '引文二'},
        ], citation_assessments: [{id: 'POL06', version: '1.0', quote: '近似引文二', condition_status: 'MET'}]}},
      },
      final_business: {
        order: {payment_status: 'CONFIRMED', paid_cents: 35900},
        case: {materials_version: 2},
        reviews: [
          {materials_version: 1, effective: false, decision: 'REJECTED'},
          {materials_version: 2, effective: true, decision: 'APPROVED'},
        ],
      },
    },
  };
  const sections = reviewSections(input);
  const [tools, citations, facts] = sections;
  assert.deepEqual(tools.items.map(({key}) => key), ['0', '1']);
  assert.match(tools.items[0].evidence[0].text, /没有输入参数/);
  assert.match(tools.items[0].evidence.at(-1).text, /程序参数比对/);
  assert.match(tools.items[1].evidence[0].text, /没有记录参数/);
  assert.deepEqual(citations.items.map(({key}) => key), ['POL06@1.0']);
  assert.deepEqual(citations.items[0].raw.citations.map(({eventId}) => eventId), ['e01', 'e04']);
  assert.doesNotMatch(JSON.stringify(citations.items), /其他版本|近似引文二/);
  assert.deepEqual(citations.items[0].evidence.filter(({label}) => label.endsWith('业务前提检查')).map(({text}) => text),
    ['条件未成立', '报告未登记这段引文的前提检查。']);
  assert.doesNotMatch(JSON.stringify(facts.items), /照片审核通过|早期说明|后来说明/);
  const closeout = reviewFacts(input.actual.final_business);
  assert.equal(closeout.material_review, '照片审核通过');
  assert.match(closeout.payment, /付款已确认.*359.00/);

  const {annotations} = buildReviewAnnotations(sections, {
    tools: {0: 'UNKNOWN', 1: 'UNSELECTED', 14: 'PASS'},
    citations: {0: 'FAIL', 1: 'PASS'},
    facts: {2: 'PASS', 3: 'FAIL'},
  });
  assert.deepEqual(annotations.tools, {'0': 'UNKNOWN'});
  assert.deepEqual(annotations.citations, {'POL06@1.0': 'FAIL'});
  assert.equal(Object.getPrototypeOf(annotations.facts), Object.prototype);
  assert.deepEqual(recordedAnnotations(annotations, 'facts'), [['__proto__', 'PASS'], ['constructor', 'FAIL']]);
  assert.equal(annotations.facts.__proto__, 'PASS');
  assert.deepEqual(buildReviewAnnotations(sections, {tools: {0: 'UNSELECTED'}}), {annotations: undefined});
});

test('legacy candidates do not create citation keys or invent missing evidence', () => {
  const sections = reviewSections({
    trace: null,
    expected: {required_facts: ['signed_at', 'receipt', 'another_fact'], required_policy_ids: ['POL06']},
    actual: {events: {e01: {output: {citations: [{id: 'POL06', version: '1.0', quote: '未进入引用审计'}]}}}},
  });
  assert.equal(sections[0].items.length, 0);
  assert.equal(sections[1].items.length, 0);
  assert.match(sections[1].empty, /历史报告缺少/);
  assert.ok(sections[2].items.every(({evidence}) => evidence[0].label === '核对位置'));
  assert.deepEqual(recordedAnnotations({tools: {'0': 'UNKNOWN', '1': 'UNSELECTED', '2': null}}, 'tools'), [['0', 'UNKNOWN']]);
});

test('review submission rejects more than 100 selections in one group without truncation', () => {
  const keys = Array.from({length: 101}, (_, index) => `fact_${index}`);
  const sections = reviewSections({expected: {required_facts: keys}, actual: {}, trace: []});
  const facts = Object.fromEntries(keys.map((_, index) => [index, 'PASS']));
  assert.deepEqual(buildReviewAnnotations(sections, {facts}), {overLimit: 'facts'});
  facts[100] = 'UNSELECTED';
  assert.equal(Object.keys(buildReviewAnnotations(sections, {facts}).annotations.facts).length, 100);
});
