import type {RunEvent} from './contracts';

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const str = (value: unknown) => typeof value === 'string' ? value : '';
const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
const records = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const finite = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : undefined;

export function claims(value: unknown) {
  return records(value).map((row) => ({
    id: str(row.id), role: str(row.role), subject: str(row.subject), predicate: str(row.predicate),
    value: str(row.value), state: str(row.state), supporting: strings(row.supporting_refs),
    conflicting: strings(row.conflicting_refs), dependencies: strings(row.depends_on_claims),
  })).filter((row) => row.id);
}

function assessments(value: unknown) {
  return records(value).map((row) => ({
    claim: str(row.claim_id), field: str(row.contradicted_field), verdict: str(row.verdict),
    refs: strings(row.evidence_refs),
  })).filter((row) => row.claim);
}

export function isInvestigationEvent(event: RunEvent) {
  return event.kind.startsWith('investigation_');
}

function evidenceRefs(value: unknown) {
  return records(value).map((ref) => ({
    id: str(ref.source_id), kind: str(ref.source_kind), version: str(ref.version_or_sha256),
  })).filter((ref) => ref.id);
}

function readEvent(event: RunEvent) {
  const p = record(event.payload);
  if (!Object.keys(p).length) return [];
  const base = {seq: event.seq, timestamp: event.created_at};
  switch (event.kind) {
    case 'investigation_route': {
      const risk = record(p.financial_risk);
      return [{...base, kind: 'route' as const, proposed: str(p.path), selected: str(p.selected_path), reason: str(p.reason),
        features: strings(p.complexity_features), amount: finite(risk.maximum_refundable_cents),
        unsettled: typeof risk.has_unsettled_refund === 'boolean' ? risk.has_unsettled_refund : undefined,
        businessControls: risk.requires_existing_business_controls === true,
        refs: records(p.evidence_refs).map((ref) => str(ref.source_id)).filter(Boolean)}];
    }
    case 'investigation_input':
      return [{...base, kind: 'phase' as const, role: str(p.role), phase: str(p.phase_id),
        upstream: strings(p.upstream_claim_ids), permittedTools: strings(p.tools)}];
    case 'investigation_tool':
      return [{...base, kind: 'tool' as const, role: str(p.role), phase: str(p.phase_id),
        tool: str(p.tool), origin: str(p.origin), refs: strings(p.evidence_ids), evidence: evidenceRefs(p.evidence_refs)}];
    case 'investigation_escalation':
      return [{...base, kind: 'escalation' as const, from: str(p.from), to: str(p.to), reason: str(p.reason)}];
    case 'investigation_recheck':
      return [{...base, kind: 'recheck' as const, role: str(p.target_role), executor: str(p.executor_role), targets: strings(p.claim_ids),
        invalidated: strings(p.stale_claim_ids), tool: str(p.tool), refs: strings(p.evidence_ids),
        reason: str(p.reason), counterexamples: assessments(p.counterexamples)}];
    case 'investigation_claim_revision':
      return [{...base, kind: 'revision' as const, role: str(p.target_role), reason: str(p.reason),
        retired: claims(p.retired_claims), replacements: claims(p.replacement_claims),
        targets: strings(p.target_claim_ids), stale: strings(p.unresolved_stale_claim_ids)}];
    case 'investigation_material_unread':
      return [{...base, kind: 'unread' as const, refs: strings(p.evidence_ids), reason: str(p.reason)}];
    case 'investigation_draft_blocked':
      return [{...base, kind: 'blocked' as const, option: str(p.option_id), reason: str(p.code)}];
    case 'investigation_claims':
      return [{...base, kind: 'claims' as const, role: str(p.role), phase: str(p.phase_id), claims: claims(p.claims)}];
    case 'investigation_assessment':
      return [{...base, kind: 'assessment' as const, claims: claims(p.claims), assessments: assessments(p.assessments),
        recheckCount: finite(p.recheck_count), stale: strings(p.stale_claim_ids)}];
    case 'investigation_result':
      return [{...base, kind: 'result' as const, claims: claims(p.claims), assessments: assessments(p.assessments),
        options: strings(p.selected_option_ids),
        proposed: records(p.proposed_options).map((option) => ({id: str(option.id), action: str(option.action)})).filter((option) => option.id),
        checks: Object.entries(record(p.program_checks)).map(([id, code]) => ({id, code: str(code)})).filter((check) => check.code)}];
    default:
      return [];
  }
}

export function investigationTrace(events: RunEvent[]) {
  const steps = events.map(readEvent).flat();
  return steps.map((step, index) => {
    if (step.kind !== 'assessment' || step.recheckCount !== 1) return step;
    const preceding = steps.slice(0, index).reverse();
    const recheck = preceding.find((item) => item.kind === 'recheck');
    const original = preceding.find((item) => item.kind === 'assessment' && item.recheckCount === 0);
    if (!recheck || original?.kind !== 'assessment' || original.seq >= recheck.seq) return step;
    const before = new Map(original.claims.map((claim) => [claim.id, claim]));
    const kept: string[] = [], revised: string[] = [];
    for (const claim of step.claims) {
      const previous = before.get(claim.id);
      if (!previous) continue;
      if (JSON.stringify(previous) === JSON.stringify(claim)) kept.push(claim.id);
      else if (recheck.targets.includes(claim.id)) revised.push(claim.id);
    }
    return {...step, kept, revised};
  });
}
