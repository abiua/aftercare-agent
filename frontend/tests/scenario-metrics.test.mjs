import assert from 'node:assert/strict';
import test from 'node:test';
import { readEvaluationLocation, writeEvaluationLocation } from '../src/evaluationRoute.ts';
import { contributionReason, matchesContributors, matchesScenarioSummary, scenarioQuery, scenarioUnit } from '../src/scenarioPresentation.ts';

test('scenario deep link retains its metric and exact case repetition only within an experiment', () => {
  const location = {tab: 'experiments', left: undefined, right: undefined, experiment: 'one', caseId: 'A01',
    repetition: 3, scenario: '__unregistered__', scenarioIntent: 'damaged', metric: 'task_success'};
  const href = writeEvaluationLocation('https://localhost/?unrelated=1', location);
  assert.deepEqual(readEvaluationLocation(new URL(href).search), location);
  assert.equal(new URL(href).searchParams.get('unrelated'), '1');
  assert.equal(new URL(writeEvaluationLocation(href, {...location, tab: 'compare'})).searchParams.has('eval_metric'), false);
  assert.equal(readEvaluationLocation('?eval=experiments&eval_experiment=e&eval_metric=..%2Fx').metric, undefined);
});

test('scope parameters omit empty values and unknown wording stays conservative', () => {
  assert.equal(scenarioQuery({scenario: null, intent: null}).toString(), '');
  assert.equal(scenarioQuery({scenario: '__unregistered__', intent: 'damaged'}).toString(), 'scenario=__unregistered__&intent=damaged');
  assert.equal(scenarioUnit('millisecond'), '毫秒');
  assert.equal(scenarioUnit('__proto__'), '个样本');
  assert.equal(contributionReason('AUDIT_MISSING'), '缺少可核对的审计记录');
  assert.equal(contributionReason('constructor'), '查看原始统计依据');
});

test('stale basis, scope, metric, experiment and page cannot populate current contributors', () => {
  const report = {experiment_id: 'exp', scope: {scenario: 'D01', intent: null}, basis_revision: 'revision-2'};
  assert.equal(matchesScenarioSummary(report, 'exp', report.scope), true);
  assert.equal(matchesScenarioSummary(report, 'exp', {scenario: 'D02', intent: null}), false);
  const data = {...report, metric: {key: 'task_success'}, limit: 20, offset: 0};
  assert.equal(matchesContributors(data, report, 'task_success', 0, 20), true);
  assert.equal(matchesContributors(data, report, 'coverage', 0, 20), false);
  assert.equal(matchesContributors(data, report, 'task_success', 20, 20), false);
  for (const change of [
    {basis_revision: 'revision-1'}, {scope: {scenario: null, intent: null}}, {experiment_id: 'other'}, {limit: 10},
  ]) {
    assert.equal(matchesContributors({...data, ...change}, report, 'task_success', 0, 20), false);
  }
});
