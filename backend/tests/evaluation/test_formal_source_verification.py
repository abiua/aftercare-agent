"""Controlled native snapshot checks. No database service, private cases or provider calls."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.evaluation.formal import native
from aftercare.evaluation.formal.contracts import (
    Candidate,
    FormalFreezeDraftV2,
    FreezeConfirmation,
    FreezeQuota,
    NativeValidation,
    NativeValidationSnapshot,
    Unlock,
    ValidationPlan,
    parse_freeze_draft,
)
from aftercare.evaluation.formal.files import json_bytes
from aftercare.evaluation.formal.register import CampaignRegister, _hash, _sha
from aftercare.evaluation.retrieval_evidence import SCHEMA_VERSION
from aftercare.intelligence.models import TRANSPORT_EVIDENCE_VERSION

CONDITIONS = ["single/economy", "single/analysis", "single/routed", "multi/routed"]
STAMP = datetime(2026, 1, 2, tzinfo=UTC)
CATALOGUE_SHA = "a" * 64
DOCUMENT_KEY = "b" * 64
POLICY = {"schema_version": SCHEMA_VERSION, "selector_sha256": "c" * 64,
          "cases_sha256": digest({"V1": CATALOGUE_SHA, "V2": CATALOGUE_SHA})}
RETRIEVAL = {
    "schema_version": SCHEMA_VERSION, "mode": "BM25", "encoder": None,
    "query_prefix_sha256": None, "dimensions": None, "distance": None,
    "lexical": {"tokenizer": "jieba.cut_for_search", "k1": 1.2, "b": 0.75},
    "fusion": {"algorithm": "rrf", "k": 60, "limit": 5},
}


def _fixtures(change=None):
    expected = {"intent": "damaged"}
    cases = {
        case: {
            "case_sha256": char * 64,
            "reference_sha256": digest(expected),
            "model_call_expectations": {
                condition: "WAITING_INPUT" if case == "V2" and condition != "multi/routed"
                else "MODEL_REQUIRED" for condition in CONDITIONS
            },
        }
        for case, char in (("V1", "d"), ("V2", "e"))
    }
    if change == "legacy":
        cases["V1"].pop("model_call_expectations")
    if change == "old_gate_event":
        cases["V2"]["model_call_expectations"]["single/economy"] = "POLICY_BLOCKED"
    configs, reports, evidence, experiments = {}, {}, {}, []
    for index, condition in enumerate(CONDITIONS, 1):
        mode, strategy = condition.split("/")
        experiment_id = str(UUID(int=index))
        config = {
            "source": "live", "split": "validation", "dataset_id": "v0.2",
            "mode": mode, "strategy": strategy, "max_calls": 2, "budget_usd": 0.1,
            "code_digest": "a" * 64, "scorer_version": "scorer", "metrics_version": "metrics",
            "input_digest": digest({case: item["case_sha256"] for case, item in cases.items()}),
            "reference_digest": digest({case: item["reference_sha256"] for case, item in cases.items()}),
            "model_snapshot": {
                "version": "registry", "registry_digest": "f" * 64,
                "models": [{"alias": "analysis", "provider": "openai", "model": "example"}],
            },
            "policy_snapshot": POLICY, "retrieval_snapshot": RETRIEVAL,
        }
        configs[condition] = digest(config)
        results = []
        for number, case in enumerate(cases, 1):
            result_id = str(UUID(int=index * 10 + number))
            run_id = str(UUID(int=index * 100 + number))
            gate = case == "V2" and condition != "multi/routed"
            cycle = 0
            binding = {
                "cycle": cycle, "digest": "6" * 64, "policy_resolution_sha256": "7" * 64,
                "selected_document_keys": [DOCUMENT_KEY],
            }
            raw_binding = {"run_id": run_id, "seq": 1, **binding}
            gate_events = [{"run_id": run_id, "seq": 1, "kind": "context_binding", "payload": binding}]
            if change == "gate_context" and index == 1 and gate:
                gate_events = []
            trace = [{"event_id": "run", "op": "start"}, {"seq": 1, "kind": "context_binding", "payload": binding}]
            isolated = {
                "catalogue": {"catalogue_sha256": CATALOGUE_SHA, "keys": [DOCUMENT_KEY], "bindings": [raw_binding]},
                "index": None, "gate_events": gate_events,
            }
            actual = {
                "observed_source": "no_provider_call" if gate else "live_provider",
                "model_audit_complete": True,
                "model_attempts": [],
                "retrieval_evidence": {
                    "schema_version": SCHEMA_VERSION, "catalogue_sha256": CATALOGUE_SHA,
                    "selector_sha256": POLICY["selector_sha256"], "configured_mode": "BM25",
                    "encoder_manifest_sha256": None, "index": {"state": "NOT_APPLICABLE"},
                },
            }
            attempts = []
            if gate:
                gate_record = {"id": run_id, "cycle": cycle, "status": "WAITING_INPUT", "reason": "WAITING_INPUT",
                               "output": {"reason": "WAITING_INPUT", "action": None, "lines": [], "missing_materials": ["ITEM"]}}
                if change == "old_gate_event" and index == 1:
                    gate_record = {**gate_record, "status": "NEEDS_REVIEW", "reason": "UNRESOLVED_POLICY",
                                   "output": {"reason": "UNRESOLVED_POLICY", "action": None, "lines": []}}
                    binding["cycle"] = gate_record["cycle"] = 1
                    trace[-1]["seq"] = raw_binding["seq"] = gate_events[0]["seq"] = 3
                    raw_binding["cycle"] = 1
                    old_event = {"seq": 2, "kind": "policy_blocked", "payload": {"blockers": ["POLICY_REQUIRED"]}}
                    trace.append(old_event)
                    gate_events.append({"run_id": run_id, **old_event})
                final = {"seq": 4, "kind": "status",
                         "payload": {key: gate_record[key] for key in ("status", "reason", "output")}}
                trace.append(final)
                gate_events.append({"run_id": run_id, **final})
                actual["events"] = {"run": gate_record}
                isolated["gate_runs"] = [
                    {**gate_record, "reason": "MATERIAL_REVIEW_REQUIRED"}
                    if change == "gate_changed" and index == 1 else gate_record
                ]
            else:
                trace.append({
                    "kind": "retrieval", "seq": 2,
                    "payload": {
                        "run_id": run_id, "cycle": cycle, "role": "verifier", "stage": "verification", "rework": 0,
                        "mode": "BM25", "snapshot": binding["policy_resolution_sha256"],
                        "retrieval_evidence": {
                            "schema_version": "ac-retrieval-query-v2", "query_sha256": "8" * 64,
                            "candidate_keys": [DOCUMENT_KEY], "vector_index": None,
                        },
                    },
                })
                marker = {
                    "version": TRANSPORT_EVIDENCE_VERSION, "path": "DEFAULT_SYNC", "response": "SDK_RETURNED",
                }
                if change == "injected" and index == 1:
                    marker.update(path="INJECTED_SYNC", response="UNOBSERVED")
                usage = {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60}
                cost = None if change == "cost_unknown" and index == 1 else "0.001"
                metadata = {"alias": "analysis", "provider": "openai", "model": "example",
                            "registry_version": "registry", "reserved_cost_usd": "0.05",
                            "base_registry_digest": config["model_snapshot"]["registry_digest"],
                            "native_streaming": False, "transport_evidence": marker}
                identity = {"run_id": run_id, "cycle": cycle, "logical_id": "verification", "ordinal": 1}
                attempts = [{**identity, "status": "SUCCEEDED", "finished_at": STAMP,
                             "metadata_json": metadata, "usage": usage, "estimated_cost_usd": cost}]
                actual["model_attempts"] = [{**identity, **metadata, "status": "SUCCEEDED",
                                             "usage": usage, "estimated_cost_usd": cost}]
                if change == "registry_unbound" and index == 1:
                    actual["model_attempts"][0].pop("base_registry_digest")
                if change == "registry_mismatch" and index == 1:
                    attempts[0]["metadata_json"]["base_registry_digest"] = "0" * 64
            evidence[result_id] = (([run_id], attempts), isolated)
            status = "NEEDS_REVIEW" if gate or index > 1 else "FAILED"
            finished_at = STAMP.isoformat()
            if change == "unsettled" and index == 1 and case == "V1":
                status, finished_at = "RUNNING", None
            results.append({
                "id": result_id, "case_id": case, "repetition": 1, "expected": expected,
                "status": status, "machine_status": status, "started_at": STAMP.isoformat(),
                "finished_at": finished_at, "actual": actual, "trace": trace, "reviews": [],
                "quota": {"calls": 1, "budget_usd": "0.05", "case_hash": cases[case]["case_sha256"],
                          "policy_catalogue_sha256": CATALOGUE_SHA},
            })
        reports[experiment_id] = {"id": experiment_id, "status": "COMPLETED", "version": 3, "config": config,
                                  "counts": {"planned": 2, "started": 2}, "results": results}
        if mode == "single":
            experiments.append({"group": "A", "experiment_id": experiment_id})
        if strategy == "routed":
            experiments.append({"group": "B", "experiment_id": experiment_id})
    plan = ValidationPlan(
        dataset_id="v0.2", cases=cases, repetitions=1, config_sha256=configs,
        policies_digest=digest(POLICY), retrieval_digest=digest(RETRIEVAL),
    )
    request = NativeValidation(
        plan_sha256=_sha(plan), experiments=experiments, selected_experiment_id=experiments[0]["experiment_id"],
        rationale="controlled", selection={"actor": "synthetic", "at": STAMP, "source": "controlled"},
    )
    return SimpleNamespace(plan=plan, request=request, reports=reports, evidence=evidence, cases=cases)


def _snapshot(fixture):
    return native.build_snapshot(fixture.plan, fixture.request, fixture.reports, fixture.evidence.__getitem__)


GATE = {
    "id": "synthetic", "version": "1", "name": "Synthetic", "source": "fixture",
    "thresholds": {"min_task_success": 0.7, "min_independent_cases": 3},
}


def _with_controls(fixture, change=None):
    config_hashes = {}
    for report in fixture.reports.values():
        config = report["config"]
        config.update(
            probe_intent=False, dependency_lock_sha256="a" * 64,
            embedding_enabled=False, embedding_model=None,
            gate_policy=GATE, gate_policy_id=GATE["id"], case_ids=["V1", "V2"],
            repetitions=1, concurrency=1, execution_driver="inline", load_reads=None,
        )
        if change:
            change(config)
        config_hashes[f"{config['mode']}/{config['strategy']}"] = digest(config)
    fixture.plan = fixture.plan.model_copy(update={"config_sha256": config_hashes})
    fixture.request = fixture.request.model_copy(update={"plan_sha256": _sha(fixture.plan)})
    return fixture


def _draft_data(candidate, plan, snapshot):
    return {
        "schema_version": "ac-formal-freeze-draft-v2",
        "candidate_sha256": _sha(candidate),
        "manifest_sha256": candidate.manifest_sha256,
        "validation_sha256": _sha(snapshot),
        "validation_plan_sha256": _sha(plan),
        "selected_validation_experiment_id": snapshot.selected_experiment_id,
        "gate_policy": GATE,
        "group_b_strategy": "routed",
        "block_order": list(reversed(CONDITIONS)),
        "conditions": {
            key: {
                "validation_experiment_id": value.validation_experiment_id,
                "controls_sha256": value.controls_sha256,
                "quota": {"calls": 3, "budget_microusd": 300},
            }
            for key, value in snapshot.controls_by_condition.items()
        },
        "row_order": "REPETITION_THEN_CASE_ID_ASC",
        "repetitions": 1,
        "case_count": 3,
        "row_quota": {"calls": 1, "budget_microusd": 100},
        "campaign_quota": {"calls": 12, "budget_microusd": 1_200},
    }


@pytest.fixture
def formal_draft(tmp_path):
    fixture = _with_controls(_fixtures())
    report = _snapshot(fixture)
    candidate = Candidate(
        purpose="formal", dataset_id="synthetic-new", manifest_sha256="2" * 64,
        distribution=dict.fromkeys(("damaged", "wrong_item", "refund_status"), 1),
        grouping_evidence="synthetic only", custodian=fixture.request.selection,
        exposed_before_freeze=False,
    )
    register = CampaignRegister(tmp_path.resolve() / "register")
    register.register("controlled", candidate)
    register.plan("controlled", fixture.plan)
    record = register.validation_native("controlled", fixture.request, lambda *_: report)
    assert set(record["controls_by_condition"]) == set(CONDITIONS)
    return register, candidate, fixture.plan, report, _draft_data(candidate, fixture.plan, report)


@pytest.mark.parametrize("command", ["check-draft", "freeze"])
def test_cli_parses_v2_draft_without_opening_or_freezing_candidate(
    formal_draft, tmp_path, monkeypatch, capsys, command,
):
    import json

    from aftercare.evaluation.formal.__main__ import main

    register, candidate, _, _, raw = formal_draft
    draft = parse_freeze_draft(raw)
    record = tmp_path / "draft.json"
    record.write_bytes(json_bytes(draft.model_dump(mode="json")))
    args = ["formal", "--root", str(register.root), command,
            "--campaign", "controlled", "--record", str(record)]
    if command == "freeze":
        confirmation = tmp_path / "confirmation.json"
        confirmation.write_bytes(json_bytes(FreezeConfirmation(
            draft_sha256=_sha(draft), decision="CONFIRMED", human=candidate.custodian,
        ).model_dump(mode="json")))
        args += ["--confirmation", str(confirmation)]
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setattr(
        "aftercare.evaluation.formal.register._inspect_package",
        lambda *_: pytest.fail("draft command must not read candidate bytes"),
    )
    if command == "check-draft":
        assert main() == 0
        result = json.loads(capsys.readouterr().out)
        assert result["draft_sha256"] == _sha(draft)
        assert result["status"] == "DRAFT_VALIDATED_ONLY"
        assert result["execution_cap_status"] == "UNCONFIGURED"
        assert result["freeze_available"] is result["execution_available"] is False
    else:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
        assert "FORMAL_EXECUTION_CAP_UNAVAILABLE" in capsys.readouterr().err
    assert not (register.root / "controlled" / "freeze.json").exists()
    assert not (register.root / "controlled" / "exposure.jsonl").exists()


def test_control_projection_allows_new_cases_references_and_policy_contents():
    fixture = _with_controls(_fixtures())
    report = _snapshot(fixture)
    assert set(report.controls_by_condition) == set(CONDITIONS)
    assert sum(row.group == "B" for row in report.reports) == 2
    control = report.controls_by_condition["single/routed"]
    source = fixture.reports[str(control.validation_experiment_id)]["config"]
    holdout = {
        **source,
        "dataset_id": "synthetic-new", "split": "holdout", "case_ids": ["H1"],
        "input_digest": "1" * 64, "reference_digest": "2" * 64,
        "policy_snapshot": {**source["policy_snapshot"], "cases_sha256": "3" * 64},
        "max_calls": 10, "budget_usd": 0.4, "repetitions": 3,
    }
    assert digest(holdout) != control.config_sha256
    assert native.project_runtime_controls(holdout) == control.controls
    assert _sha(native.project_runtime_controls(holdout)) == control.controls_sha256


@pytest.mark.parametrize(("field", "value"), [
    ("probe_intent", True), ("dependency_lock_sha256", "1" * 64),
    ("code_digest", "2" * 64), ("scorer_version", "other"), ("metrics_version", "other"),
    ("model_snapshot", {"version": "same", "models": ["other"], "registry_digest": "1" * 64}),
    ("policy_snapshot", {**POLICY, "selector_sha256": "1" * 64}),
    ("retrieval_snapshot", {**RETRIEVAL, "lexical": {"k1": 1}}),
])
def test_classified_control_drift_changes_the_projection(field, value):
    fixture = _with_controls(_fixtures())
    config = next(iter(fixture.reports.values()))["config"]
    original = native.project_runtime_controls(config)
    changed = native.project_runtime_controls({**config, field: value})
    assert original is not None and changed is not None
    assert _sha(changed) != _sha(original)


@pytest.mark.parametrize("change", [
    lambda config: config.pop("dependency_lock_sha256"),
    lambda config: config.update(dependency_lock_sha256=None),
    lambda config: config.update(future_unclassified_control=True),
])
def test_legacy_or_unclassified_configs_keep_source_and_cannot_supply_formal_controls(change):
    report = _snapshot(_with_controls(_fixtures(), change))
    assert report.source_verification == "NATIVE_VALIDATION_VERIFIED"
    serialized = report.model_dump(mode="json")
    assert "controls_by_condition" not in serialized
    assert _sha(NativeValidationSnapshot.model_validate(serialized)) == _hash(json_bytes(serialized))


def test_validated_draft_keeps_explicit_order_and_public_release_hard_blocked(formal_draft, tmp_path):
    register, _, _, _, raw = formal_draft
    draft = parse_freeze_draft(raw)
    assert draft.block_order == list(reversed(CONDITIONS))
    response = register.check_draft("controlled", draft)
    assert response["status"] == "DRAFT_VALIDATED_ONLY"
    assert response["execution_cap_status"] == "UNCONFIGURED"
    assert response["freeze_available"] is response["execution_available"] is False
    cap = FreezeQuota(calls=12, budget_microusd=1_200)
    assert register.check_draft("controlled", draft, execution_cap=cap)["execution_cap_status"] == "WITHIN_CAP"
    with pytest.raises(BusinessError, match="超过"):
        register.check_draft("controlled", draft, execution_cap=FreezeQuota(calls=11, budget_microusd=1_200))
    confirmation = FreezeConfirmation(
        draft_sha256=_sha(draft), decision="CONFIRMED",
        human={"actor": "synthetic", "at": STAMP, "source": "fixture only"},
    )
    with pytest.raises(BusinessError) as blocked:
        register.freeze("controlled", draft, confirmation)
    assert blocked.value.code == "FORMAL_EXECUTION_UNAVAILABLE"
    with pytest.raises(BusinessError) as blocked:
        register.unlock("controlled", Unlock(
            freeze_sha256="1" * 64, directory=str(tmp_path / "missing"), handoff=confirmation.human,
        ))
    assert blocked.value.code == "FORMAL_UNLOCK_BLOCKED"
    assert register.preflight("controlled")["freeze_sha256"] is None


@pytest.mark.parametrize("change", [
    lambda raw: raw.update(schema_version="ac-formal-freeze-v2"),
    lambda raw: raw.update(block_order=[CONDITIONS[0]] * 4),
    lambda raw: raw.update(group_b_strategy="economy"),
    lambda raw: raw["conditions"]["multi/routed"].update(
        validation_experiment_id=raw["conditions"]["single/routed"]["validation_experiment_id"]
    ),
    lambda raw: raw["row_quota"].update(calls=True),
    lambda raw: raw["row_quota"].update(budget_microusd=100.0),
    lambda raw: raw["campaign_quota"].update(budget_microusd=1_199),
    lambda raw: raw["conditions"]["single/routed"]["quota"].update(calls=4),
])
def test_freeze_draft_schema_rejects_invalid_order_matrix_or_budget(formal_draft, change):
    *_, raw = formal_draft
    change(raw)
    with pytest.raises(ValidationError):
        parse_freeze_draft(raw)


@pytest.mark.parametrize("change", [
    lambda raw: raw.update(manifest_sha256="4" * 64),
    lambda raw: raw.update(validation_plan_sha256="4" * 64),
    lambda raw: raw.update(selected_validation_experiment_id=UUID(int=4)),
    lambda raw: raw["conditions"]["single/routed"].update(controls_sha256="4" * 64),
    lambda raw: raw.update(gate_policy={**GATE, "version": "another"}),
])
def test_valid_shape_does_not_override_locked_validation_or_candidate(formal_draft, change):
    register, *_, raw = formal_draft
    change(raw)
    with pytest.raises(BusinessError) as blocked:
        register.check_draft("controlled", FormalFreezeDraftV2.model_validate(raw))
    assert blocked.value.code == "FORMAL_FREEZE_MISMATCH"


def test_complete_native_provenance_preserves_quality_failure_and_human_declaration():
    report = _snapshot(_fixtures())
    assert report.source_verification == "NATIVE_VALIDATION_VERIFIED"
    assert report.human_identity == report.selection.identity_verification == "UNAUTHENTICATED_DECLARATION"
    selected = report.evidence[report.selected_experiment_id]
    assert selected.machine_statuses == {"FAILED": 1, "NEEDS_REVIEW": 1}
    assert selected.findings["NO_RECORDED_ATTEMPT"] == 1
    assert selected.source_blockers == []
    assert list(report.evidence.values())[-1].findings["PRODUCTION_SDK_RESPONSE_MATCHED"] == 2


@pytest.mark.parametrize(("change", "reason"), [
    ("legacy", "MODEL_CALL_EXPECTATION_MISSING"),
    ("registry_unbound", "ATTEMPT_REGISTRY_DIGEST_UNBOUND"),
    ("registry_mismatch", "ATTEMPT_REGISTRY_DIGEST_MISMATCH"),
    ("injected", "INJECTED_TRANSPORT_ATTEMPTS"),
    ("cost_unknown", "ATTEMPT_COST_UNKNOWN"),
    ("unsettled", "CASE_RUN_NOT_SETTLED"),
    ("gate_context", "EXPECTED_ZERO_CALL_GATE_UNVERIFIED"),
    ("gate_changed", "EXPECTED_ZERO_CALL_GATE_UNVERIFIED"),
    ("old_gate_event", "EXPECTED_ZERO_CALL_GATE_UNVERIFIED"),
])
def test_each_unresolved_source_condition_blocks_promotion(change, reason):
    report = _snapshot(_fixtures(change))
    assert report.source_verification == "NATIVE_LEDGER_ONLY"
    assert reason in report.evidence[report.selected_experiment_id].source_blockers


def test_new_retrieval_findings_fail_closed(monkeypatch):
    original = native.native_retrieval_evidence

    def evidence(row, config, isolated, findings):
        findings["UNRECOGNIZED_FUTURE_FINDING"] += 1
        return original(row, config, isolated, findings)

    monkeypatch.setattr(native, "native_retrieval_evidence", evidence)
    report = _snapshot(_fixtures())
    assert report.source_verification == "NATIVE_LEDGER_ONLY"
    assert "UNRECOGNIZED_FUTURE_FINDING" in report.evidence[report.selected_experiment_id].source_blockers


def test_legacy_expectation_absence_preserves_digest():
    fixture = _fixtures("legacy")
    expected = {**fixture.plan.model_dump(mode="json"), "cases": fixture.cases}
    assert fixture.plan.model_dump(mode="json") == expected
    assert _sha(fixture.plan) == _hash(json_bytes(expected))


def test_active_parent_cannot_qualify_even_if_every_result_is_settled():
    fixture = _fixtures()
    fixture.reports[str(fixture.request.selected_experiment_id)]["status"] = "RUNNING"
    with pytest.raises(BusinessError) as error:
        _snapshot(fixture)
    assert error.value.code == "FORMAL_VALIDATION_MISMATCH"


def test_native_audit_digest_distinguishes_changed_registry_evidence():
    fixture = _fixtures("registry_mismatch")
    before = _snapshot(fixture)
    for raw, _ in fixture.evidence.values():
        for attempt in raw[1]:
            if attempt["metadata_json"].get("base_registry_digest") == "0" * 64:
                attempt["metadata_json"]["base_registry_digest"] = "1" * 64
    after = _snapshot(fixture)
    selected = before.selected_experiment_id
    assert before.source_verification == after.source_verification == "NATIVE_LEDGER_ONLY"
    assert before.evidence[selected].source_blockers == after.evidence[selected].source_blockers
    assert before.evidence[selected].audit_sha256 != after.evidence[selected].audit_sha256
