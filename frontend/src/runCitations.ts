import type { CitationAssessment, RunCitation, RunOutput } from './contracts';

const conditionLabels: Record<CitationAssessment['condition_status'], string> = {
  MET: '前提有记录支持',
  UNMET: '条件未成立',
  UNKNOWN: '证据不足',
  NOT_ASSESSED: '条件未评估',
};

export function sameCitation(citation: RunCitation, candidate: {id?: unknown; version?: unknown; quote?: unknown}) {
  return candidate.id === citation.id && candidate.version === citation.version && candidate.quote === citation.quote;
}

export function describeCitationCondition(reported: unknown) {
  const status = typeof reported === 'string' && Object.hasOwn(conditionLabels, reported)
    ? reported as CitationAssessment['condition_status'] : 'NOT_ASSESSED';
  return {status, label: conditionLabels[status]};
}

export function describeRunCitations(output: Pick<RunOutput, 'citations' | 'citation_assessments'>) {
  const entries = (output.citations ?? []).map((citation) => {
    const assessment = output.citation_assessments?.find((item) => sameCitation(citation, item));
    return {citation, assessment, ...describeCitationCondition(assessment?.condition_status)};
  });
  return {
    supported: entries.filter(({status}) => status === 'MET'),
    background: entries.filter(({status}) => status !== 'MET'),
  };
}
