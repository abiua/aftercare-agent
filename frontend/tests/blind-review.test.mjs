import assert from 'node:assert/strict';
import {test} from 'node:test';
import {latestBlindDetail, visibleBlindExposure} from '../src/chatBlindReview.ts';
import {readEvaluationLocation, writeEvaluationLocation} from '../src/evaluationRoute.ts';

test('blind exposure requires both a recorded initial judgment and explicit revealed stage', () => {
  const exposure = {candidates: [{id: 'internal-candidate'}]};
  const initial = {payload: {judgment: 'UNDETERMINED'}};
  assert.equal(visibleBlindExposure({status: 'BLIND', initial: null, exposure}), null);
  assert.equal(visibleBlindExposure({status: 'SEALED', initial, exposure}), null);
  assert.equal(visibleBlindExposure({status: 'REVEALED', initial: null, exposure}), null);
  assert.equal(visibleBlindExposure({status: 'REVEALED', initial, exposure}), exposure);
});

test('an older GET cannot reopen a sealed form and blind links ignore ordinary results', () => {
  const first = {id: 'one', version: 1, status: 'BLIND'};
  const sealed = {...first, version: 2, status: 'SEALED'};
  assert.equal(latestBlindDetail(first, sealed), sealed);
  assert.equal(latestBlindDetail({...sealed, version: 3}, sealed).version, 3);
  const route = readEvaluationLocation('?eval=blind&eval_experiment=sample&eval_case=one&eval_repeat=1');
  assert.equal(route.tab, 'blind');
  assert.equal(route.experiment, undefined);
  assert.equal(route.caseId, undefined);
  assert.equal(writeEvaluationLocation('https://example.test/?eval_experiment=sample', {...route, experiment: 'sample'}),
    'https://example.test/?eval=blind');
});
