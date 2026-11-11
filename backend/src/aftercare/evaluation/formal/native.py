"""Verify native validation evidence without treating provenance as quality approval."""

import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations

from pydantic import ValidationError
from sqlalchemy import inspect, select

from aftercare.business.rules import digest, utc
from aftercare.intelligence.models import TRANSPORT_EVIDENCE_VERSION
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

from ..gates import GatePolicy
from ..retrieval_evidence import (
    SCHEMA_VERSION,
    compare_case_retrieval,
    native_retrieval_evidence,
    read_native_retrieval,
)
from ..runner import _namespace
from ..supplemental_usage import _identity, changes
from .contracts import (
    ConditionControls,
    NativeExperimentEvidence,
    NativeReportSnapshot,
    NativeValidationSnapshot,
    RuntimeControls,
)
from .files import fail
from .register import _pairs, _sha, _validate_matrix


def _require(condition):
    if not condition:
        fail("FORMAL_VALIDATION_MISMATCH", "原生验证实验、输入全集、冻结配置或配额与计划不一致。")


def _money(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def read_isolated(connection, result_id):
    """Use the caller's read-only PostgreSQL snapshot and a derived schema name."""
    namespace = _namespace(result_id)
    inspector = inspect(connection)
    execution = {"schema_translate_map": {None: namespace}}
    audit = None
    gate_runs, gate_events = [], []
    if inspector.has_table(ModelAttempt.__tablename__, schema=namespace):
        run_ids = []
        if inspector.has_table(AgentRun.__tablename__, schema=namespace):
            gate_runs = [
                dict(row) for row in connection.execute(
                    select(AgentRun.id, AgentRun.cycle, AgentRun.status, AgentRun.reason, AgentRun.output)
                    .order_by(AgentRun.id),
                    execution_options=execution,
                ).mappings()
            ]
            run_ids = [row["id"] for row in gate_runs]
        if run_ids and inspector.has_table(RunEvent.__tablename__, schema=namespace):
            gate_events = [
                dict(row) for row in connection.execute(
                    select(RunEvent.run_id, RunEvent.seq, RunEvent.kind, RunEvent.payload)
                    .where(RunEvent.kind.in_(("context_binding", "policy_blocked", "refund_lookup", "status")))
                    .order_by(RunEvent.run_id, RunEvent.seq),
                    execution_options=execution,
                ).mappings()
            ]
        stmt = select(
            ModelAttempt.run_id,
            ModelAttempt.cycle,
            ModelAttempt.logical_id,
            ModelAttempt.ordinal,
            ModelAttempt.status,
            ModelAttempt.metadata_json,
            ModelAttempt.usage,
            ModelAttempt.estimated_cost_usd,
            ModelAttempt.finished_at,
        ).order_by(
            ModelAttempt.run_id, ModelAttempt.cycle, ModelAttempt.logical_id, ModelAttempt.ordinal
        )
        audit = (
            run_ids,
            [dict(row) for row in connection.execute(stmt, execution_options=execution).mappings()],
        )
    catalogue, index = read_native_retrieval(connection, inspector, namespace, execution)
    retrieval = (
        {"catalogue": catalogue, "index": index, "gate_runs": gate_runs, "gate_events": gate_events}
        if catalogue is not None or index is not None or gate_runs
        else None
    )
    return audit, retrieval


def _transport_finding(attempt, matching, eligible):
    marker = (attempt.get("metadata_json") or {}).get("transport_evidence")
    if marker is None:
        return "TRANSPORT_EVIDENCE_MISSING"
    if not isinstance(marker, dict) or marker.get("version") != TRANSPORT_EVIDENCE_VERSION:
        return "TRANSPORT_EVIDENCE_INVALID"
    path, response = marker.get("path"), marker.get("response")
    if not isinstance(path, str):
        return "TRANSPORT_EVIDENCE_INVALID"
    if path in {"INJECTED_SYNC", "INJECTED_STREAM"}:
        return "INJECTED_TRANSPORT_ATTEMPTS"
    if path not in {"DEFAULT_SYNC", "DEFAULT_STREAM"}:
        return "TRANSPORT_EVIDENCE_INVALID"
    if response == "UNOBSERVED":
        return "PRODUCTION_RESPONSE_UNOBSERVED"
    if response == "HTTP_ERROR_RESPONSE":
        status = marker.get("http_status")
        if type(status) is not int or not 400 <= status <= 599:
            return "TRANSPORT_EVIDENCE_INVALID"
    elif response != "SDK_RETURNED":
        return "TRANSPORT_EVIDENCE_INVALID"
    if (
        not eligible
        or attempt["finished_at"] is None
        or attempt["status"] == "STARTED"
        or len(matching) != 1
        or matching[0].get("transport_evidence") != marker
    ):
        return "TRANSPORT_EVIDENCE_UNMATCHED"
    return (
        "PRODUCTION_SDK_RESPONSE_MATCHED"
        if response == "SDK_RETURNED"
        else "PRODUCTION_HTTP_ERROR_MATCHED"
    )


def _check_audit(row, raw, config, statuses, findings):
    started = row["started_at"] is not None
    frozen = (row.get("actual") or {}).get("model_attempts")
    if raw is None:
        if started or frozen:
            findings["AUDIT_UNAVAILABLE"] += 1
        return None
    run_ids, attempts = raw
    if not isinstance(frozen, list):
        if started or attempts:
            findings["FROZEN_AUDIT_UNAVAILABLE"] += 1
        frozen = []
    if not started and attempts:
        findings["UNSTARTED_RESULT_WITH_ATTEMPTS"] += 1
    if not started and frozen:
        findings["UNSTARTED_RESULT_WITH_FROZEN_ATTEMPTS"] += 1
    # A sole AgentRun does not identify an attempt in the frozen result.
    # Keep rows with no persisted identity unpaired even if ordinals coincide.
    verified_attempts = [attempt for attempt in attempts if attempt["run_id"] in run_ids]
    differences, unmatched, unmatched_snapshot = changes(verified_attempts, frozen)
    unmatched += len(attempts) - len(verified_attempts)
    if differences:
        findings["LATE_OR_CHANGED_ATTEMPT"] += len(differences)
    if unmatched or unmatched_snapshot:
        findings["AUDIT_UNMATCHED"] += unmatched + unmatched_snapshot
    if started and not attempts:
        findings["NO_RECORDED_ATTEMPT"] += 1
    statuses.update(attempt["status"] for attempt in attempts)
    registry = config.get("model_snapshot") or {}
    models = {
        (model.get("alias"), model.get("provider"), model.get("model"))
        for model in registry.get("models", [])
    }
    frozen_by_id = {}
    for attempt in frozen:
        frozen_by_id.setdefault(_identity(attempt), []).append(attempt)
    raw_id_counts = Counter(_identity(attempt) for attempt in attempts)
    charges = []
    projected = []
    for attempt in attempts:
        if attempt["run_id"] not in run_ids:
            findings["AUDIT_RUN_UNKNOWN"] += 1
        metadata = attempt.get("metadata_json") or {}
        identity = tuple(metadata.get(key) for key in ("alias", "provider", "model"))
        if identity not in models:
            findings["ATTEMPT_MODEL_MISMATCH"] += 1
        key = _identity(attempt)
        matching = frozen_by_id.get(key, [])
        eligible = (
            started
            and key is not None
            and raw_id_counts[key] == 1
            and attempt["run_id"] in run_ids
            and identity in models
            and bool(registry.get("version"))
            and metadata.get("registry_version") == registry["version"]
            and all(
                tuple(item.get(field) for field in ("alias", "provider", "model")) == identity
                and item.get("registry_version") == registry["version"]
                for item in matching
            )
        )
        findings[_transport_finding(attempt, matching, eligible)] += 1
        reserve = _money(metadata.get("reserved_cost_usd"))
        cost = _money(attempt["estimated_cost_usd"])
        if cost is None:
            findings["ATTEMPT_COST_UNKNOWN"] += 1
        charge = cost if cost is not None else reserve
        if charge is None:
            findings["ATTEMPT_CHARGE_UNKNOWN"] += 1
        charges.append(charge)
        projected.append(
            {
                **{
                    key: attempt[key]
                    for key in (
                        "run_id",
                        "cycle",
                        "logical_id",
                        "ordinal",
                        "status",
                        "usage",
                        "estimated_cost_usd",
                    )
                },
                "finished_at": utc(attempt["finished_at"]).isoformat()
                if attempt["finished_at"]
                else None,
                "model_identity": identity,
                "base_registry_digest": metadata.get("base_registry_digest"),
                "reserved_cost_usd": str(reserve) if reserve is not None else None,
                "transport_evidence": metadata.get("transport_evidence"),
            }
        )
    if len(attempts) > row["quota"]["calls"] or sum(
        (charge for charge in charges if charge is not None), Decimal(0)
    ) > _money(row["quota"]["budget_usd"]):
        findings["ATTEMPT_QUOTA_EXCEEDED"] += 1
    return projected


MATCHED_RESPONSES = {"PRODUCTION_SDK_RESPONSE_MATCHED", "PRODUCTION_HTTP_ERROR_MATCHED"}
GATE_NON_FINDINGS = {"NO_RECORDED_ATTEMPT", "NO_RETRIEVAL_OBSERVED"}
CASE_TERMINAL = {"PASSED", "FAILED", "TIMEOUT", "ERROR", "NEEDS_REVIEW"}
ATTEMPT_TERMINAL = {"SUCCEEDED", "FAILED"}


def _matching_event_sequences(row, isolated, run_id, kind, check):
    frozen = {
        event["seq"]: event.get("payload")
        for event in row.get("trace", [])
        if event.get("kind") == kind and type(event.get("seq")) is int
    }
    return [
        event["seq"] for event in isolated.get("gate_events", [])
        if event["run_id"] == run_id and event["kind"] == kind
        and type(event.get("seq")) is int and isinstance(event.get("payload"), dict)
        and event["payload"] == frozen.get(event["seq"]) and check(event["payload"])
    ]


def _zero_call_gate(row, raw, isolated, expected):
    actual = row.get("actual") or {}
    if (
        raw is None or len(raw[0]) != 1 or raw[1] or actual.get("model_attempts") != []
        or actual.get("observed_source") != "no_provider_call" or not isinstance(isolated, dict)
    ):
        return False
    run_id = raw[0][0]
    runs = isolated.get("gate_runs", [])
    if len(runs) != 1 or runs[0]["id"] != run_id:
        return False
    events = actual.get("events") or {}
    steps = [
        events.get(event["event_id"])
        for event in row.get("trace", [])
        if event.get("op") in {"start", "resume"} and event.get("event_id") in events
    ]
    if not steps or not isinstance(steps[-1], dict) or steps[-1].get("id") != run_id:
        return False
    run = runs[0]
    if (
        type(run.get("cycle")) is not int
        or any(run.get(key) != steps[-1].get(key) for key in ("cycle", "status", "reason", "output"))
        or any(event.get("kind") in {"agent", "retrieval"} for event in row.get("trace", []))
    ):
        return False
    status, reason, output = run["status"], run["reason"], run.get("output")
    if (
        not isinstance(output, dict) or output.get("reason") != reason
        or "action" not in output or output["action"] is not None or output.get("lines") != []
    ):
        return False
    bindings = _matching_event_sequences(
        row, isolated, run_id, "context_binding",
        lambda item: item.get("cycle") == run["cycle"] and _digest_value(item.get("digest")),
    )
    if len(bindings) != 1:
        return False
    terminal = {"status": status, "reason": reason, "output": output}
    publications = [
        seq for seq in _matching_event_sequences(row, isolated, run_id, "status", lambda item: item == terminal)
        if seq > bindings[0]
    ]
    if len(publications) != 1:
        return False

    def marker(kind, check):
        return any(
            bindings[0] < seq < publications[0]
            for seq in _matching_event_sequences(row, isolated, run_id, kind, check)
        )
    if expected == "WAITING_INPUT":
        missing = output.get("missing_materials")
        return status == reason == expected and isinstance(missing, list) and bool(missing)
    if expected == "MATERIAL_REVIEW_REQUIRED":
        return status == "WAITING_INPUT" and reason == expected and output.get("missing_materials") == []
    if expected == "POLICY_BLOCKED":
        return (
            status == "NEEDS_REVIEW" and reason in {"HUMAN_REVIEW", "UNRESOLVED_POLICY"}
            and marker("policy_blocked", lambda item: bool(item.get("blockers")))
        )
    if expected == "TARGETED_REFUND":
        lookup = output.get("refund_lookup") or {}
        return (
            status == "COMPLETED" and reason == "STATUS_ONLY"
            and lookup.get("status") in {"FOUND", "NOT_FOUND", "AMBIGUOUS_REFERENCE", "INVALID_REFERENCE"}
            and marker("refund_lookup", lambda item: item.get("status") == lookup["status"])
        )
    return False


def _row_blockers(row, raw, retrieval, findings, expectation, registry_digest):
    blockers = set(findings) - MATCHED_RESPONSES
    if (
        row["machine_status"] not in CASE_TERMINAL
        or row.get("started_at") is None or row.get("finished_at") is None
    ):
        blockers.add("CASE_RUN_NOT_SETTLED")
    actual = row.get("actual") or {}
    if actual.get("model_audit_complete") is not True:
        blockers.add("MODEL_AUDIT_INCOMPLETE")
    frozen = {}
    snapshot_attempts = actual.get("model_attempts")
    for attempt in snapshot_attempts if isinstance(snapshot_attempts, list) else []:
        frozen.setdefault(_identity(attempt), []).append(attempt.get("base_registry_digest"))
    for attempt in raw[1] if raw else []:
        if attempt["status"] not in ATTEMPT_TERMINAL or attempt["finished_at"] is None:
            blockers.add("ATTEMPT_NOT_SETTLED")
        usage = attempt.get("usage") or {}
        if not isinstance(usage, dict) or any(
            type(usage.get(key)) is not int or usage[key] < 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        ):
            blockers.add("ATTEMPT_USAGE_UNVERIFIED")
        metadata = attempt.get("metadata_json") or {}
        digest = metadata.get("base_registry_digest")
        snapshot_digests = frozen.get(_identity(attempt), [])
        if not _digest_value(digest) or len(snapshot_digests) != 1 or not _digest_value(snapshot_digests[0]):
            blockers.add("ATTEMPT_REGISTRY_DIGEST_UNBOUND")
        elif digest != registry_digest or snapshot_digests[0] != registry_digest:
            blockers.add("ATTEMPT_REGISTRY_DIGEST_MISMATCH")
        marker = metadata.get("transport_evidence")
        native_stream = metadata.get("native_streaming")
        if isinstance(marker, dict) and marker.get("path") in {"DEFAULT_SYNC", "DEFAULT_STREAM"}:
            if type(native_stream) is not bool or native_stream != (marker["path"] == "DEFAULT_STREAM"):
                blockers.add("ATTEMPT_PATH_BINDING_MISMATCH")
    if expectation is None:
        blockers.add("MODEL_CALL_EXPECTATION_MISSING")
    elif expectation == "MODEL_REQUIRED":
        if not any(findings[code] for code in MATCHED_RESPONSES):
            blockers.add("REQUIRED_PROVIDER_RESPONSE_MISSING")
    elif _zero_call_gate(row, raw, retrieval, expectation):
        blockers -= GATE_NON_FINDINGS
    else:
        blockers.add("EXPECTED_ZERO_CALL_GATE_UNVERIFIED")
    return blockers


def _digest_value(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _config_blockers(config):
    policy = config.get("policy_snapshot") or {}
    retrieval = config.get("retrieval_snapshot") or {}
    registry = config.get("model_snapshot") or {}

    blockers = set()
    if not (
        registry.get("version") and registry.get("models") and _digest_value(registry.get("registry_digest"))
    ):
        blockers.add("FROZEN_REGISTRY_DIGEST_UNAVAILABLE")
    if not (
        policy.get("schema_version") == SCHEMA_VERSION and _digest_value(policy.get("selector_sha256"))
        and _digest_value(policy.get("cases_sha256"))
    ):
        blockers.add("FROZEN_POLICY_SNAPSHOT_UNAVAILABLE")
    mode = retrieval.get("mode")
    if not (
        retrieval.get("schema_version") == SCHEMA_VERSION
        and ((mode == "BM25" and retrieval.get("encoder") is None)
             or (mode == "HYBRID" and _digest_value((retrieval.get("encoder") or {}).get("sha256"))))
    ):
        blockers.add("FROZEN_RETRIEVAL_SNAPSHOT_UNAVAILABLE")
    return blockers


RUNTIME_CONTROL_FIELDS = frozenset({
    "mode", "strategy", "source", "probe_intent", "code_digest", "scorer_version",
    "metrics_version", "model_snapshot", "policy_snapshot", "retrieval_snapshot",
    "embedding_enabled", "embedding_model", "dependency_lock_sha256",
})
DATA_FIELDS = frozenset({
    "dataset_id", "split", "case_ids", "input_digest", "reference_digest",
})
SCHEDULE_FIELDS = frozenset({
    "repetitions", "concurrency", "max_calls", "budget_usd", "execution_driver",
    "load_reads",
})
GATE_FIELDS = frozenset({"gate_policy", "gate_policy_id"})
RETRIEVAL_RECIPE_FIELDS = frozenset({
    "schema_version", "mode", "encoder", "query_prefix_sha256", "dimensions",
    "distance", "lexical", "fusion",
})


def project_runtime_controls(config) -> RuntimeControls | None:
    """Project only classified live controls. Data and schedule have separate bindings."""
    keys = config.keys()
    if (
        not RUNTIME_CONTROL_FIELDS <= keys
        or keys - (RUNTIME_CONTROL_FIELDS | DATA_FIELDS | SCHEDULE_FIELDS | GATE_FIELDS)
        or config.get("load_reads") is not None
        or any(not isinstance(config[key], dict) for key in (
            "model_snapshot", "policy_snapshot", "retrieval_snapshot",
        ))
    ):
        return None
    model = config["model_snapshot"]
    policy = config["policy_snapshot"]
    retrieval = config["retrieval_snapshot"]
    if (
        set(policy) != {"schema_version", "selector_sha256", "cases_sha256"}
        or retrieval.keys() != RETRIEVAL_RECIPE_FIELDS
    ):
        return None
    encoder = retrieval.get("encoder")
    if (
        (encoder is not None and not isinstance(encoder, dict))
        or _config_blockers(config)
        or config["embedding_enabled"] is not (retrieval["mode"] == "HYBRID")
        or config["embedding_model"] != (encoder.get("model_id") if encoder else None)
        or (encoder is not None and config["embedding_model"] is None)
    ):
        return None
    try:
        return RuntimeControls(
            schema_version="ac-formal-runtime-controls-v1",
            mode=config["mode"],
            strategy=config["strategy"],
            source=config["source"],
            probe_intent=config["probe_intent"],
            code_digest=config["code_digest"],
            dependency_lock_sha256=config["dependency_lock_sha256"],
            scorer_version=config["scorer_version"],
            metrics_version=config["metrics_version"],
            model_snapshot_sha256=digest(model),
            registry_sha256=model["registry_digest"],
            policy_schema_version=policy["schema_version"],
            policy_selector_sha256=policy["selector_sha256"],
            retrieval_sha256=digest(retrieval),
            embedding_enabled=config["embedding_enabled"],
            embedding_model=config["embedding_model"],
        )
    except ValidationError:
        return None


def _condition_controls(reports):
    result = {}
    for report in reports:
        config = report["config"]
        controls = project_runtime_controls(config)
        if controls is None or "gate_policy" not in config:
            return None
        raw_gate = config["gate_policy"]
        try:
            gate = GatePolicy.model_validate(raw_gate) if raw_gate is not None else None
        except ValidationError:
            return None
        key = f"{controls.mode}/{controls.strategy}"
        result[key] = ConditionControls(
            validation_experiment_id=report["id"],
            config_sha256=digest(config),
            controls=controls,
            controls_sha256=_sha(controls),
            gate_policy=gate,
        )
    return result


def _evidence(report, load_isolated, case_evidence, plan_cases):
    config = report["config"]
    budget = _money(config.get("budget_usd"))
    limit = config.get("max_calls")
    _require(type(limit) is int and 1 <= limit <= 144 and budget is not None and 0 < budget <= 10)
    total_calls, total_budget = 0, Decimal(0)
    statuses, frozen_statuses = Counter(), Counter()
    findings = Counter()
    blockers = _config_blockers(config)
    for key in ("code_digest", "scorer_version", "metrics_version", "model_snapshot"):
        if not config.get(key):
            code = f"{key.upper()}_UNAVAILABLE"
            findings[code] += 1
            blockers.add(code)
    audits, indexes, quotas = {}, {}, {}
    for row in report["results"]:
        if row["machine_status"] in {"QUEUED", "RUNNING"}:
            findings["UNSETTLED_RESULTS"] += 1
        quota = row["quota"]
        fee, calls = _money(quota.get("budget_usd")), quota.get("calls")
        _require(type(calls) is int and calls >= 0 and fee is not None)
        total_calls += calls
        total_budget += fee
        quotas[row["id"]] = {"calls": calls, "budget_usd": str(fee)}
        frozen_statuses.update(
            attempt["status"] for attempt in (row.get("actual") or {}).get("model_attempts", [])
        )
        raw, retrieval = load_isolated(row["id"])
        row_findings = Counter()
        audits[row["id"]] = _check_audit(row, raw, config, statuses, row_findings)
        case_evidence[row["case_id"], row["repetition"]] = native_retrieval_evidence(
            row, config, retrieval, row_findings
        )
        condition = f"{config['mode']}/{config['strategy']}"
        expectation = (plan_cases[row["case_id"]].model_call_expectations or {}).get(condition)
        blockers.update(_row_blockers(
            row, raw, retrieval, row_findings, expectation,
            (config.get("model_snapshot") or {}).get("registry_digest"),
        ))
        findings.update(row_findings)
        if isinstance(retrieval, dict):
            index = retrieval.get("index")
            indexes[row["id"]] = {
                "catalogue": retrieval.get("catalogue"),
                "index": {key: value for key, value in index.items() if key != "rows"}
                if index
                else None,
            }
        else:
            indexes[row["id"]] = retrieval
    _require(total_calls <= limit and total_budget <= budget)
    if not any(findings[code] for code in MATCHED_RESPONSES):
        code = "PROVIDER_RESPONSE_PROVENANCE_UNAVAILABLE"
        findings[code] += 1
        blockers.add(code)
    return NativeExperimentEvidence(
        version=report["version"],
        status=report["status"],
        machine_statuses=dict(Counter(row["machine_status"] for row in report["results"])),
        effective_statuses=dict(Counter(row["status"] for row in report["results"])),
        attempt_statuses=dict(statuses),
        frozen_attempt_statuses=dict(frozen_statuses),
        findings=dict(findings),
        quota_sha256=digest(quotas),
        audit_sha256=digest(audits),
        index_sha256=digest(indexes),
        source_blockers=sorted(blockers),
    )


def build_snapshot(plan, request, reports, load_isolated, *, read_at=None):
    _require(request.plan_sha256 == _sha(plan))
    groups, evidence, snapshots = {"A": [], "B": []}, {}, []
    case_evidence = {}
    expected_pairs = {
        (case_id, repetition)
        for case_id in plan.cases
        for repetition in range(1, plan.repetitions + 1)
    }
    input_digest = digest({key: case.case_sha256 for key, case in plan.cases.items()})
    reference_digest = digest({key: case.reference_sha256 for key, case in plan.cases.items()})
    for entry in request.experiments:
        ident = str(entry.experiment_id)
        report = reports[ident]
        config = report["config"]
        sha = digest(config)
        _require(
            report["id"] == ident
            and report.get("parent_experiment_id") is None
            and report["status"] in {"COMPLETED", "FAILED", "CANCELLED"}
            and config.get("source") == "live"
            and config.get("split") == "validation"
            and config.get("dataset_id") == plan.dataset_id
            and config.get("input_digest") == input_digest
            and config.get("reference_digest") == reference_digest
            and plan.config_sha256.get(f"{config.get('mode')}/{config.get('strategy')}") == sha
            and len(report["results"]) == len(expected_pairs)
            and _pairs(report) == expected_pairs
        )
        for row in report["results"]:
            case = plan.cases[row["case_id"]]
            _require(
                row["quota"].get("case_hash") == case.case_sha256
                and digest(row["expected"]) == case.reference_sha256
            )
        policy_snapshot, retrieval_snapshot = (
            config.get("policy_snapshot"),
            config.get("retrieval_snapshot"),
        )
        if policy_snapshot is not None or retrieval_snapshot is not None:
            by_case = {
                row["case_id"]: row["quota"].get("policy_catalogue_sha256")
                for row in report["results"]
            }
            _require(
                bool(policy_snapshot and retrieval_snapshot)
                and all(
                    row["quota"].get("policy_catalogue_sha256") == by_case[row["case_id"]]
                    for row in report["results"]
                )
                and all(by_case.values())
                and policy_snapshot.get("cases_sha256") == digest(by_case)
                and digest(policy_snapshot) == plan.policies_digest
                and digest(retrieval_snapshot) == plan.retrieval_digest
            )
        groups[entry.group].append(report)
        if ident not in evidence:
            case_evidence[ident] = {}
            evidence[ident] = _evidence(report, load_isolated, case_evidence[ident], plan.cases)
        snapshots.append(
            NativeReportSnapshot(
                group=entry.group,
                experiment_id=ident,
                report_sha256=digest(
                    {key: report[key] for key in ("id", "version", "config", "results")}
                ),
                config_sha256=sha,
                source="live",
                mode=config["mode"],
                strategy=config["strategy"],
                counts=report["counts"],
                code_digest=config.get("code_digest"),
                scorer_version=config.get("scorer_version"),
                metrics_version=config.get("metrics_version"),
                model_snapshot_digest=digest(config["model_snapshot"])
                if config.get("model_snapshot")
                else None,
            )
        )
    _validate_matrix(groups, str(request.selected_experiment_id))
    compared = set()
    for group in groups.values():
        for first, second in combinations(group, 2):
            left, right = first["id"], second["id"]
            pair = tuple(sorted((left, right)))
            if pair in compared:
                continue
            compared.add(pair)
            for key in case_evidence[left].keys() & case_evidence[right].keys():
                for finding in compare_case_retrieval(
                    case_evidence[left][key], case_evidence[right][key]
                ):
                    for ident in (left, right):
                        counts = evidence[ident].findings
                        counts[finding] = counts.get(finding, 0) + 1
                        blockers = evidence[ident].source_blockers or []
                        evidence[ident].source_blockers = sorted({*blockers, finding})
    verified = all(item.source_blockers == [] for item in evidence.values())
    return NativeValidationSnapshot(
        source_verification="NATIVE_VALIDATION_VERIFIED" if verified else "NATIVE_LEDGER_ONLY",
        plan_sha256=request.plan_sha256,
        reports=snapshots,
        selected_experiment_id=str(request.selected_experiment_id),
        rationale=request.rationale,
        selection=request.selection,
        read_at=read_at or datetime.now(UTC),
        evidence=evidence,
        controls_by_condition=_condition_controls(
            {report["id"]: report for group in groups.values() for report in group}.values()
        ) if verified else None,
    )
