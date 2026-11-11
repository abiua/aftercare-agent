import assert from 'node:assert/strict';
import {test} from 'node:test';
import {investigationReason} from '../src/investigationMessages.ts';

test('staff stop reasons distinguish prompt size, unavailable model, changed inputs, and uncertain spend', () => {
  assert.match(investigationReason('INVESTIGATION_PROMPT_LIMIT'), /超过一次调查可处理的范围/);
  assert.match(investigationReason('INVESTIGATION_MODEL_UNAVAILABLE'), /没有可用于本轮调查的模型/);
  assert.match(investigationReason('INVESTIGATION_PROMPT_VERSION_CHANGED'), /配置版本已更新/);
  assert.match(investigationReason('INVESTIGATION_EVIDENCE_INCONSISTENT'), /证据记录不一致/);
  assert.match(investigationReason('INVESTIGATION_ISSUE_COST_UNKNOWN'), /费用尚无法确认/);
  assert.match(investigationReason('INVESTIGATION_ISSUE_COST_LIMIT'), /预算已用尽/);
  assert.equal(investigationReason('INVESTIGATION_UNKNOWN_FUTURE'), undefined);
  assert.equal(investigationReason('constructor'), undefined);
});
