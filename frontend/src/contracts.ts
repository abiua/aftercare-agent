export type Role = 'customer' | 'support' | 'approver' | 'tester';
export type User = {user_id: string; display_name: string; role: Role; customer_id?: string};
export type Runtime = {
  mode: 'offline' | 'live';
  models: {alias: string; model: string; available: boolean}[];
  features: Record<string, boolean>;
};

export type Intent = 'damaged' | 'wrong_item' | 'refund_status' | 'cancel' | 'return' | 'warranty';
export type OrderLine = {
  line_id: string; product_id: string; quantity: number; paid_cents: number; refunded_cents: number;
};
export type Order = {
  id: string; version: number; customer_id: string; status: string; items: OrderLine[];
  products: {id: string; name: string}[]; shipment?: unknown; created_at?: string;
};
export type Case = {
  id: string; ticket_id: string; order_id: string; customer_id: string;
  intent: Intent; description: string; status: string; version: number;
  materials_version: number; line_ids: string[]; created_at: string;
};
export type IntakeIntent = Extract<Intent, 'damaged' | 'wrong_item' | 'refund_status'>;
export type IntakeClassifier = 'rules' | 'model';
export type IntakeCapabilities = {rules: boolean; model: boolean};
export type IntakeOrder = {id: string; version: number; status: string};
export type IntakePreviewRequest = {
  message: string; order_id?: string; intent?: IntakeIntent; classifier: IntakeClassifier;
};
export type IntakePreview = {
  status: 'READY' | 'CLARIFICATION_REQUIRED';
  required: ('order' | 'intent')[];
  order: IntakeOrder | null;
  order_candidates: IntakeOrder[];
  intent: IntakeIntent | null;
  intent_candidates: IntakeIntent[];
  classifier_source: 'rules' | 'confirmed' | 'model' | 'model_unavailable';
  reason: string | null;
};
export type Material = {id: string; kind: string; filename?: string; content_type?: string; created_at: string};
export type MaterialReview = {
  id: string; materials_version: number; decision: string; notes: string; reviewer_id: string; created_at: string;
};
export type Action = 'REFUND' | 'REPLACE';
export type ProposalLine = {line_id: string; amount_cents?: number; quantity?: number};
export type Proposal = {
  id: string; version: number; action: Action; lines: ProposalLine[]; notes: string;
  policy_ids: string[]; payload_hash: string; status: string; amount_cents: number; created_at: string;
};
export type Approval = {
  id: string; proposal_id: string; proposal_version: number; decision: string; notes: string;
  approver_id?: string; expires_at: string | null; created_at: string; effective: boolean;
};
export type Execution = {
  id: string; case_id?: string; proposal_id?: string; action: Action; status: string; amount_cents: number;
  lines: ProposalLine[]; receipt: unknown; business_key?: string; created_at: string;
};
export type Policy = {id: string; title?: string; version?: string | number; text?: string; content?: string};
export type CaseDetail = {
  refund_lookup_available?: boolean;
  case: Case; order: Order; policies: Policy[]; materials: Material[]; reviews: MaterialReview[];
  ledger: Execution[]; versions: Record<string, unknown>; facts: Record<string, unknown>; proposals: Proposal[]; approvals: Approval[];
  eligibility: {
    status: string; actions: Action[]; reason?: string; reasons?: string[]; missing_materials?: string[]; policy_ids: string[];
    lines: {line_id: string; available_cents: number; product_id: string; replace_available_quantity: number | null}[];
  };
};

export type Mode = 'single' | 'multi';
export type Strategy = 'economy' | 'analysis' | 'routed';
export type RefundLookupExecution = {
  id: string; status: string; amount_cents: number; created_at: string; receipt_confirmed: boolean;
};
export type RefundLookupResult =
  | {status: 'FOUND'; execution: RefundLookupExecution}
  | {status: 'REFERENCE_REQUIRED' | 'AMBIGUOUS_REFERENCE' | 'INVALID_REFERENCE' | 'NOT_FOUND'; execution: null};
export type RefundLookupResponse = {schema_version: string} & Extract<RefundLookupResult, {status: 'FOUND'}>;
export type RunCitation = {id: string; version: string; quote: string};
export type CitationAssessment = RunCitation & {
  use: 'REUSE_EXISTING_REFUND' | 'RECONCILE_UNKNOWN_REFUND' | null;
  condition_status: 'MET' | 'UNMET' | 'UNKNOWN' | 'NOT_ASSESSED';
  reason_code: string; registry_version: string;
  scope: {kind: 'ORDER_REFUND_EXECUTION_LEDGER' | 'CURRENT_CASE_EXECUTION_LEDGER' | 'MATCHED_REFUND_EXECUTION' | null; execution_ids: string[]};
};
export type RunOutput = {
  action: Action | null; lines: ProposalLine[]; policy_ids: string[]; notes: string;
  missing_materials: string[]; reason: string; citations?: RunCitation[];
  citation_assessments?: CitationAssessment[];
  historical_refunded_cents?: number | null; refund_lookup?: RefundLookupResult;
};
export type Run = {
  id: string; case_id: string; thread_id: string; status: string;
  mode: Mode; strategy: Strategy; case_version: number; output?: RunOutput | null;
  reason?: string | null; version?: number; created_at: string; updated_at: string;
};
export type RunEvent = {seq: number; kind: string; payload: Record<string, unknown>; created_at: string};
export type BusinessEvent = {sequence: number; type: string; payload: Record<string, unknown>; created_at: string};
export type Items<T> = {items: T[]};

export type EvidenceSource = 'fixture' | 'live' | 'fault' | 'load';
export type Dataset = {id: string; name: string; version: string; case_count: number; source?: string};
export type EvalCase = {
  id: string; dataset_id: string; intent: Intent; difficulty: string; split: string;
  scenario: string; review_status: string; input: unknown; expected: unknown; snapshot?: unknown;
};
export type ExperimentConfig = {
  dataset_id: string; split?: string; mode: Mode; strategy: Strategy; repetitions: number; concurrency: number;
  max_calls: number; budget_usd: number; source: EvidenceSource; gate_policy_id?: string | null;
};
export type Metric = {
  key: string; label: string; value: number | null; numerator: number | null; denominator: number | null;
  unit: 'ratio' | 'ms' | 'usd' | 'count'; source: string;
};
export type ScenarioScope = {scenario: string | null; intent: string | null};
export type ScenarioMetric = Metric & {
  sample_unit: string; measured_samples: number; unique_cases: number; unknown: number | null; evidence_unknown?: number;
  unknown_includes_measured?: boolean; unknown_unit: string; denominator_unit?: string | null; drilldown_supported: boolean; metric_version?: string;
};
export type ScenarioSummary = {
  schema_version: string; experiment_id: string; dataset_id: string | null; config_hash: string;
  frozen_metrics_version: string | null; computed_metrics_version: string; basis_revision: string;
  status: 'AVAILABLE' | 'HISTORICAL_DEFINITION_UNAVAILABLE'; scope: ScenarioScope;
  scenario_options: {key: string; label: string}[]; intent_options: {key: string; label: string}[];
  scope_counts: {planned: number; started: number; finished: number; not_run: number; unique_cases: number; repeated_runs: number};
  metrics: ScenarioMetric[];
};
export type ContributionBucket = 'included' | 'attention' | 'unknown' | 'excluded';
export type ScenarioContribution = {
  bucket: ContributionBucket; reason: string; measured?: boolean; value?: number | null;
  numerator?: number | null; denominator?: number | null; evidence?: Record<string, unknown>;
};
export type ScenarioContributors = {
  schema_version: string; experiment_id: string; scope: ScenarioScope; basis_revision: string;
  metric: ScenarioMetric; bucket_counts: Record<ContributionBucket, number>;
  items: {
    result_id: string; case_id: string; repetition: number; machine_status: string;
    effective_status: string; review_version: number | null; contributions: ScenarioContribution[];
  }[];
  total: number; limit: number; offset: number;
};
export type EvaluationCapabilities = {
  eval_import?: boolean; eval_retry?: boolean; eval_gates?: boolean; eval_supplemental_usage?: boolean;
  eval_scenario_metrics?: boolean; eval_progress_events?: boolean;
};
export type ReleaseThresholds = {
  min_coverage?: number | null; min_task_success?: number | null; min_independent_cases?: number | null;
  min_human_gold?: number | null; max_total_cost_usd?: number | string | null;
  max_cost_per_success?: number | string | null; max_final_violation_rate?: number | null; require_live?: boolean;
};
export type ReleasePolicy = {id: string; version: string; name: string; thresholds: ReleaseThresholds; required_checks: string[]};
export type ReviewDecision = 'PASS' | 'FAIL' | 'UNKNOWN';
export type ReviewAnnotations = Record<'tools' | 'citations' | 'facts', Record<string, ReviewDecision>>;
export type EvalReview = {
  id: string; decision: string; notes: string; reviewer_id: string; created_at: string;
  version?: number; annotations?: Partial<ReviewAnnotations>;
};
export type EvalResult = {
  id: string; case_id: string; repetition: number; status: string; machine_status?: string; version: number; intent?: Intent;
  expected: unknown; actual: unknown; checks: unknown; trace: unknown; reviews: EvalReview[];
  failure_reason?: string | null; retry_of_result_id?: string | null;
};
export type ProviderTokenUsage = {
  input_tokens?: number; output_tokens?: number; total_tokens?: number;
  input_token_details?: {cache_read?: number; cache_creation?: number; audio?: number};
  output_token_details?: {reasoning?: number; audio?: number};
};
export type SupplementalUsageAttempt = {
  run_id: string; cycle: number; logical_id: string; ordinal: number; status: string;
  usage: ProviderTokenUsage | null; estimated_cost_usd: string | null; finished_at: string | null;
  snapshot_status: string | null; snapshot_usage: ProviderTokenUsage | null;
  snapshot_estimated_cost_usd: string | null;
};
export type SupplementalUsageReport = {
  result_id: string; as_of: string; status: 'AVAILABLE' | 'UNAVAILABLE';
  unavailable_reason: 'RESULT_NOT_FINISHED' | 'SNAPSHOT_FORMAT_UNSUPPORTED' | 'SCHEMA_NOT_FOUND'
    | 'AUDIT_UNREADABLE' | 'AUDIT_STORAGE_UNSUPPORTED' | null;
  unmatched_attempt_count: number; unmatched_snapshot_attempt_count: number; attempts: SupplementalUsageAttempt[];
};
export type Experiment = {
  id: string; name: string; status: string; config: ExperimentConfig; version: number; created_at: string;
  parent_experiment_id?: string | null;
  counts: {planned: number; started: number; finished: number; not_run: number; failed?: number; cancelled?: number; timed_out?: number};
  metrics: Metric[]; results: EvalResult[];
  gate: {status: string; checks: {name: string; status: string; reason: string}[];
    policy_id?: string | null; policy?: ReleasePolicy | null; release_status?: string | null};
};
export type ImportedReport = {
  id: string; sha256: string; origin: string; source: 'imported_unverified'; created_at: string;
  name: string; status: string; counts: Experiment['counts']; report?: unknown;
};
export type Comparison = {
  compatible: boolean; reasons: string[]; pairing?: {planned: number; finished: number; complete: boolean};
  rows: {case_id: string; repetition: number; left_status: string; right_status: string; change: string}[];
  metrics?: {
    key: string; label: string; unit: Metric['unit']; left: number | null; right: number | null;
    left_denominator?: number | null; right_denominator?: number | null;
  }[];
};
