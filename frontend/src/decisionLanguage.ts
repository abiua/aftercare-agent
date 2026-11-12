import type {ProgramCheck} from './chatDecisionData';

export function decisionCheck(check?: ProgramCheck): string {
  if (check?.status === 'PASSED') return '规则检查通过，等待客服审核';
  if (check?.status === 'NON_EXECUTING') return '等待客服跟进';
  if (check?.code === 'PARTIAL_RESOLUTION_REQUIRED') return '这些商品需要分开处理，请客服调整方案';
  return '方案暂不能通过，请客服核对';
}
