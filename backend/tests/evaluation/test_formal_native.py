from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from aftercare.auth import UserContext
from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.evaluation.formal import native as native_module
from aftercare.evaluation.formal.contracts import Candidate, NativeValidation, ValidationPlan
from aftercare.evaluation.formal.native import build_snapshot
from aftercare.evaluation.formal.register import CampaignRegister, _sha
from aftercare.evaluation.service import EvaluationService


@pytest.fixture
def native():
    conditions = (
        ("single", "economy"),
        ("single", "analysis"),
        ("single", "routed"),
        ("multi", "routed"),
    )
    reports = {}
    configs = {}
    experiments = []
    reference = {"intent": "damaged"}
    cases = {
        case: {"case_sha256": char * 64, "reference_sha256": digest(reference)}
        for case, char in (("V1", "a"), ("V2", "b"))
    }
    for index, (mode, strategy) in enumerate(conditions, 1):
        ident = str(UUID(int=index))
        config = {
            "source": "live",
            "split": "validation",
            "dataset_id": "v0.2",
            "mode": mode,
            "strategy": strategy,
            "max_calls": 2,
            "budget_usd": 0.1,
            "code_digest": "a" * 64,
            "scorer_version": "scorer",
            "embedding_enabled": True,
            "input_digest": digest({key: case["case_sha256"] for key, case in cases.items()}),
            "reference_digest": digest(
                {key: case["reference_sha256"] for key, case in cases.items()}
            ),
            "model_snapshot": {
                "version": "registry",
                "models": [{"alias": "economic", "provider": "test", "model": "test-model"}],
            },
        }
        configs[f"{mode}/{strategy}"] = digest(config)
        reports[ident] = {
            "id": ident,
            "status": "COMPLETED",
            "version": 7,
            "config": config,
            "counts": {"planned": 2, "started": 1, "finished": 1, "not_run": 1},
            "results": [
                {
                    "id": str(UUID(int=100 + index * 2 + i)),
                    "case_id": case,
                    "repetition": 1,
                    "started_at": "now" if i == 0 else None,
                    "expected": reference,
                    "reviews": [],
                    "quota": {
                        "case_hash": cases[case]["case_sha256"],
                        "calls": 1,
                        "budget_usd": "0.05",
                    },
                    "machine_status": "TIMEOUT" if i == 0 else "NOT_RUN",
                    "status": "TIMEOUT" if i == 0 else "NOT_RUN",
                    "actual": {
                        "model_attempts": [
                            {
                                "run_id": "run",
                                "cycle": 0,
                                "logical_id": "logical",
                                "ordinal": 1,
                                "status": "UNKNOWN",
                                "estimated_cost_usd": None,
                            }
                        ]
                    }
                    if i == 0
                    else None,
                }
                for i, case in enumerate(cases)
            ],
        }
        if index < 4:
            experiments.append({"group": "A", "experiment_id": ident})
        if strategy == "routed":
            experiments.append({"group": "B", "experiment_id": ident})
    plan = ValidationPlan(
        dataset_id="v0.2",
        cases=cases,
        repetitions=1,
        config_sha256=configs,
        policies_digest="c" * 64,
        retrieval_digest="d" * 64,
    )
    request = NativeValidation(
        plan_sha256=_sha(plan),
        experiments=experiments,
        selected_experiment_id=experiments[0]["experiment_id"],
        rationale="synthetic only",
        selection={"actor": "fixture", "at": "2026-01-01T00:00:00Z", "source": "test"},
    )
    return SimpleNamespace(plan=plan, request=request, reports=reports)


def _attempt_fixture():
    return (
        (
            ["run"],
            [
                {
                    "run_id": "run",
                    "cycle": 0,
                    "logical_id": "logical",
                    "ordinal": 1,
                    "status": "UNKNOWN",
                    "usage": None,
                    "estimated_cost_usd": None,
                    "finished_at": None,
                    "metadata_json": {
                        "alias": "economic",
                        "provider": "test",
                        "model": "test-model",
                        "reserved_cost_usd": "0.05",
                    },
                }
            ],
        ),
        [{"key": "policy", "encoder": "old-name"}],
    )


def _audit(result_id):
    # The fixture allocates even result IDs to started rows.
    return _attempt_fixture() if UUID(result_id).int % 2 == 0 else (None, None)


def test_native_snapshot_keeps_failed_and_missing_evidence(native):
    snapshot = build_snapshot(native.plan, native.request, native.reports, _audit)
    assert snapshot.source_verification == "NATIVE_LEDGER_ONLY"
    assert len(snapshot.reports) == 5 and len(snapshot.evidence) == 4
    for item in snapshot.evidence.values():
        assert item.machine_statuses == {"TIMEOUT": 1, "NOT_RUN": 1}
        assert item.attempt_statuses == item.frozen_attempt_statuses == {"UNKNOWN": 1}
        assert item.findings == {
            "PROVIDER_RESPONSE_PROVENANCE_UNAVAILABLE": 1,
            "INDEX_PROVENANCE_UNAVAILABLE": 1,
            "RETRIEVAL_EVIDENCE_UNAVAILABLE": 1,
            "METRICS_VERSION_UNAVAILABLE": 1,
            "ATTEMPT_COST_UNKNOWN": 1,
            "TRANSPORT_EVIDENCE_MISSING": 1,
        }
    assert snapshot.human_identity == "UNAUTHENTICATED_DECLARATION"


@pytest.mark.parametrize(
    "changed", ["missing_pair", "case_hash", "quota", "configuration", "retry"]
)
def test_native_snapshot_refuses_mismatched_frozen_plans(native, changed):
    reports = deepcopy(native.reports)
    first = next(iter(reports.values()))
    if changed == "missing_pair":
        first["results"].pop()
    elif changed == "case_hash":
        first["results"][0]["quota"]["case_hash"] = "e" * 64
    elif changed == "quota":
        first["results"][0]["quota"]["budget_usd"] = "0.1"
    elif changed == "configuration":
        first["config"]["max_calls"] = 99
    else:
        first["parent_experiment_id"] = "original"
    with pytest.raises(BusinessError) as error:
        build_snapshot(native.plan, native.request, reports, _audit)
    assert error.value.code == "FORMAL_VALIDATION_MISMATCH"


def test_native_local_lock_snapshot_readback_has_no_source_promotion(tmp_path, native):
    register = CampaignRegister(tmp_path.resolve() / "register")
    human = native.request.selection
    candidate = Candidate(
        purpose="formal",
        dataset_id="holdout-new",
        manifest_sha256="9" * 64,
        distribution={"damaged": 1, "wrong_item": 1, "refund_status": 1},
        grouping_evidence="test fixture only",
        custodian=human,
        exposed_before_freeze=False,
    )
    register.register("campaign", candidate)
    register.plan("campaign", native.plan)
    recorded = register.validation_native(
        "campaign",
        native.request,
        lambda plan, request: build_snapshot(plan, request, native.reports, lambda _: (None, None)),
    )
    preflight = register.preflight("campaign")
    assert preflight["native_validation_sha256"] == recorded["native_validation_sha256"]
    assert preflight["source_verification"] == "NATIVE_LEDGER_ONLY"
    assert preflight["status"] == "REGISTERED" and not preflight["execution_available"]
    assert all(item["findings"]["AUDIT_UNAVAILABLE"] == 1 for item in recorded["evidence"].values())
    with pytest.raises(BusinessError) as error:
        register.validation_native(
            "campaign", native.request, lambda *_: pytest.fail("Snapshot reread")
        )
    assert error.value.code == "FORMAL_RECORD_EXISTS"


@pytest.mark.parametrize(
    "role,profile,code",
    [
        ("support", "test", "EVALUATION_FORBIDDEN"),
        ("tester", "app", "EVALUATION_REQUIRES_TEST"),
        ("tester", "test", "FORMAL_NATIVE_POSTGRES_REQUIRED"),
    ],
)
def test_native_service_does_not_read_outside_guard(role, profile, code):
    engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    service = EvaluationService(engine, SimpleNamespace(profile=profile))
    with pytest.raises(BusinessError) as error:
        service.native_validation_snapshot(UserContext(user_id="actor", role=role), None, None)
    assert error.value.code == code


def test_late_attempt_does_not_overwrite_frozen_unknown(native):
    def late(result_id):
        raw, index = _audit(result_id)
        if raw is not None:
            raw[1][0].update(status="SUCCEEDED", estimated_cost_usd="0.06")
        return raw, index

    snapshot = build_snapshot(native.plan, native.request, native.reports, late)
    for item in snapshot.evidence.values():
        assert item.attempt_statuses == {"SUCCEEDED": 1}
        assert item.frozen_attempt_statuses == {"UNKNOWN": 1}
        assert item.findings["LATE_OR_CHANGED_ATTEMPT"] == 1
        assert item.findings["ATTEMPT_QUOTA_EXCEEDED"] == 1


def test_lone_agent_run_never_fills_missing_frozen_attempt_id(native):
    for report in native.reports.values():
        report["results"][0]["actual"]["model_attempts"][0].pop("run_id")

    def late(result_id):
        raw, index = _audit(result_id)
        if raw is not None:
            raw[1][0].update(status="SUCCEEDED", estimated_cost_usd="0.03")
        return raw, index

    snapshot = build_snapshot(native.plan, native.request, native.reports, late)
    for item in snapshot.evidence.values():
        assert item.attempt_statuses == {"SUCCEEDED": 1}
        assert item.frozen_attempt_statuses == {"UNKNOWN": 1}
        assert item.findings["AUDIT_UNMATCHED"] == 2
        assert "LATE_OR_CHANGED_ATTEMPT" not in item.findings


def test_unstarted_result_probes_schema_and_charges_orphan_attempt(native):
    probes = []
    first = next(iter(native.reports.values()))
    orphan = first["results"][1]["id"]

    def loader(result_id):
        probes.append(result_id)
        if result_id == orphan:
            raw, index = _attempt_fixture()
            raw[1][0].update(status="UNKNOWN", estimated_cost_usd=None)
            raw[1][0]["metadata_json"]["reserved_cost_usd"] = "0.06"
            return ([], raw[1]), index
        return _audit(result_id)

    snapshot = build_snapshot(native.plan, native.request, native.reports, loader)
    assert set(probes) == {
        row["id"] for report in native.reports.values() for row in report["results"]
    }
    affected = snapshot.evidence[first["id"]]
    assert affected.attempt_statuses == {"UNKNOWN": 2}
    assert affected.findings["UNSTARTED_RESULT_WITH_ATTEMPTS"] == 1
    assert affected.findings["ATTEMPT_QUOTA_EXCEEDED"] == 1
    assert affected.findings["ATTEMPT_COST_UNKNOWN"] == 2
    assert affected.findings["AUDIT_RUN_UNKNOWN"] == 1
    assert affected.findings["AUDIT_UNMATCHED"] == 1
    assert all("AUDIT_UNAVAILABLE" not in item.findings for item in snapshot.evidence.values())


def test_cancelled_parent_with_running_result_stays_unsettled(native):
    first = next(iter(native.reports.values()))
    first["status"] = "CANCELLED"
    first["results"][0].update(status="RUNNING", machine_status="RUNNING")
    snapshot = build_snapshot(native.plan, native.request, native.reports, _audit)
    affected = snapshot.evidence[first["id"]]
    assert affected.status == "CANCELLED" and affected.machine_statuses["RUNNING"] == 1
    assert affected.findings["UNSETTLED_RESULTS"] == 1
    assert affected.attempt_statuses == affected.frozen_attempt_statuses == {"UNKNOWN": 1}
    assert snapshot.source_verification == "NATIVE_LEDGER_ONLY"


def test_isolated_probe_reads_attempt_table_without_agent_run_table(monkeypatch):
    result_id = str(UUID(int=42))
    namespace = "eval_" + UUID(result_id).hex
    probes = []
    attempt = _attempt_fixture()[0][1][0]

    def has_table(name, *, schema):
        assert schema == namespace
        probes.append(name)
        return name == "agent_model_attempts"

    def execute(statement, *, execution_options):
        assert execution_options == {"schema_translate_map": {None: namespace}}
        assert "agent_model_attempts" in str(statement)
        return SimpleNamespace(mappings=lambda: [attempt])

    connection = SimpleNamespace(execute=execute)
    monkeypatch.setattr(
        native_module, "inspect", lambda value: SimpleNamespace(has_table=has_table)
    )
    assert native_module.read_isolated(connection, result_id) == (([], [attempt]), None)
    assert "agent_runs" in probes


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("production", "PRODUCTION_SDK_RESPONSE_MATCHED"),
        ("http-error", "PRODUCTION_HTTP_ERROR_MATCHED"),
        ("injected", "INJECTED_TRANSPORT_ATTEMPTS"),
        ("late", "TRANSPORT_EVIDENCE_UNMATCHED"),
        ("wrong-registry", "TRANSPORT_EVIDENCE_UNMATCHED"),
    ],
)
def test_native_transport_provenance_requires_matching_frozen_model_and_response(
    native, variant, expected
):
    marker = {
        "version": "ac-attempt-transport-v1",
        "path": "DEFAULT_SYNC",
        "response": "SDK_RETURNED",
    }
    if variant == "http-error":
        marker.update(response="HTTP_ERROR_RESPONSE", http_status=401)
    if variant == "injected":
        marker.update(path="INJECTED_SYNC", response="UNOBSERVED")
    binding = {
        "alias": "economic",
        "provider": "test",
        "model": "test-model",
        "registry_version": "registry",
    }
    for report in native.reports.values():
        frozen = report["results"][0]["actual"]["model_attempts"][0]
        frozen.update(
            binding,
            status="FAILED",
            transport_evidence=(
                {**marker, "response": "UNOBSERVED"} if variant == "late" else marker
            ),
        )

    def read(result_id):
        raw, index = _audit(result_id)
        if raw is not None:
            attempt = raw[1][0]
            attempt.update(status="FAILED", finished_at=datetime.now(UTC))
            attempt["metadata_json"].update(binding, transport_evidence=marker)
            if variant == "wrong-registry":
                attempt["metadata_json"]["registry_version"] = "another"
        return raw, index

    snapshot = build_snapshot(native.plan, native.request, native.reports, read)
    for item in snapshot.evidence.values():
        assert item.findings[expected] == 1
        assert item.attempt_statuses == item.frozen_attempt_statuses == {"FAILED": 1}
        assert ("PROVIDER_RESPONSE_PROVENANCE_UNAVAILABLE" in item.findings) == (
            variant not in {"production", "http-error"}
        )
    assert snapshot.source_verification == "NATIVE_LEDGER_ONLY"


def test_native_freeze_and_paired_indexes_use_actual_case_evidence(native):
    from aftercare.evaluation.retrieval_evidence import SCHEMA_VERSION
    from aftercare.intelligence.policies import QUERY_EVIDENCE_VERSION
    from aftercare.intelligence.retrieval_evidence import MODEL_ID, vector_index_evidence

    reports = deepcopy(native.reports)
    key, catalogue_sha = "b" * 64, "d" * 64
    policies = {
        "schema_version": SCHEMA_VERSION,
        "selector_sha256": "e" * 64,
        "cases_sha256": digest({case_id: catalogue_sha for case_id in native.plan.cases}),
    }
    encoder_sha = "f" * 64
    retrieval = {
        "schema_version": SCHEMA_VERSION,
        "mode": "HYBRID",
        "encoder": {"sha256": encoder_sha},
    }
    configs, isolated = {}, {}
    for index, (ident, report) in enumerate(reports.items()):
        config = report["config"]
        config.update(policy_snapshot=policies, retrieval_snapshot=retrieval)
        configs[f"{config['mode']}/{config['strategy']}"] = digest(config)
        for row in report["results"]:
            row["quota"]["policy_catalogue_sha256"] = catalogue_sha
            if row["started_at"] is None:
                continue
            rows = [(key, f"{MODEL_ID}@{encoder_sha}", [2.0 if index == 0 else 1.0] * 512)]
            index_evidence = vector_index_evidence(rows)
            run_id = f"run-{row['id']}"
            binding = {
                "cycle": 0,
                "policy_resolution_sha256": "c" * 64,
                "selected_document_keys": [key],
            }
            row["actual"]["retrieval_evidence"] = {
                "schema_version": SCHEMA_VERSION,
                "selector_sha256": policies["selector_sha256"],
                "catalogue_sha256": catalogue_sha,
                "configured_mode": "HYBRID",
                "encoder_manifest_sha256": encoder_sha,
                "index": {**index_evidence, "state": "BUILT"},
            }
            row["trace"] = [
                {
                    "kind": "context_binding",
                    "seq": 1,
                    "payload": binding,
                },
                {
                    "kind": "retrieval",
                    "seq": 2,
                    "payload": {
                        "mode": "HYBRID",
                        "run_id": run_id,
                        "cycle": 0,
                        "stage": "policy",
                        "role": "single" if config["mode"] == "single" else "policy",
                        "rework": 0,
                        "snapshot": binding["policy_resolution_sha256"],
                        "retrieval_evidence": {
                            "schema_version": QUERY_EVIDENCE_VERSION,
                            "query_sha256": "a" * 64,
                            "candidate_keys": [key],
                            "vector_index": index_evidence,
                            "query_vector_sha256": "7" * 64,
                        },
                    },
                },
            ]
            isolated[row["id"]] = {
                "catalogue": {
                    "keys": [key],
                    "catalogue_sha256": catalogue_sha,
                    "bindings": [{"run_id": run_id, "seq": 1, **binding}],
                },
                "index": {**index_evidence, "rows": rows},
            }
    plan = native.plan.model_copy(
        update={
            "config_sha256": configs,
            "policies_digest": digest(policies),
            "retrieval_digest": digest(retrieval),
        }
    )
    request = native.request.model_copy(update={"plan_sha256": _sha(plan)})
    result = build_snapshot(plan, request, reports, lambda ident: (None, isolated.get(ident)))
    first = result.evidence[str(request.selected_experiment_id)]
    assert first.findings["PAIRED_INDEX_CONTENT_MISMATCH"] == 2
    assert first.findings["PAIRED_QUERY_INDEX_MISMATCH"] == 2
    assert "INDEX_CONTENT_MISMATCH" not in first.findings
    assert "QUERY_POLICY_BINDING_MISMATCH" not in first.findings
    assert "QUERY_EVIDENCE_UNAVAILABLE" not in first.findings
    legacy = deepcopy(reports)
    legacy_trace = legacy[str(request.selected_experiment_id)]["results"][0]["trace"]
    legacy_trace[-1]["payload"]["retrieval_evidence"]["schema_version"] = "ac-retrieval-query-v1"
    old = build_snapshot(plan, request, legacy, lambda ident: (None, isolated.get(ident)))
    old_first = old.evidence[str(request.selected_experiment_id)]
    assert old_first.findings["QUERY_EVIDENCE_UNAVAILABLE"] == 1
    assert old_first.findings["PAIRED_INDEX_CONTENT_MISMATCH"] == 2
    assert "PAIRED_QUERY_INDEX_MISMATCH" not in old_first.findings
    altered = plan.model_copy(update={"policies_digest": "0" * 64})
    with pytest.raises(BusinessError, match="原生验证"):
        build_snapshot(
            altered,
            request.model_copy(update={"plan_sha256": _sha(altered)}),
            reports,
            lambda _: (None, None),
        )
