import assert from 'node:assert/strict';
import {test} from 'node:test';
import {decisionCheck} from '../src/decisionLanguage.ts';

test('blocked and pending decisions never become approvals in display', () => {
 assert.equal(decisionCheck({status:'BLOCKED',code:'PARTIAL_RESOLUTION_REQUIRED'}), '这些商品需要分开处理，请客服调整方案');
 assert.match(decisionCheck({status:'PASSED'}), /等待客服审核/);
});
