from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from aftercare.business.errors import BusinessError
from aftercare.evaluation.formal import register as formal
from aftercare.evaluation.formal.contracts import (
    Candidate,
    FreezeConfirmation,
    FreezeDraft,
    HumanRecord,
    Unlock,
    Validation,
    ValidationPlan,
)
from aftercare.evaluation.formal.files import json_bytes, json_data


def _save(path, value):
    raw = json_bytes(value)
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.fixture
def campaign(tmp_path):
    root = tmp_path.resolve()
    private = root / "private-package"
    private.mkdir()
    governance = root / "governance"
    store = formal.CampaignRegister(governance)
    evidence = HumanRecord(
        actor="fixture-operator", at="2026-01-01T00:00:00Z", source="synthetic test only"
    )
    distribution = dict.fromkeys(("damaged", "wrong_item", "refund_status"), 1)
    refs = {}
    cases = []
    for index, intent in enumerate(distribution):
        ident = f"H-{index}"
        cases.append(
            {
                "id": ident,
                "split": "holdout",
                "message": f"synthetic phrase {index}",
                "fixture": {},
                "events": [{"id": "1", "op": "create_case", "args": {"intent": intent}}],
            }
        )
        refs[ident] = {"intent": intent, "family": f"synthetic-{index}"}
    manifest = {
        "schema_version": "ac-formal-package-v1",
        "dataset_id": "synthetic-1",
        "cases_sha256": _save(private / "cases.json", cases),
        "references_sha256": _save(private / "references.json", refs),
    }
    candidate = Candidate(
        purpose="fixture",
        dataset_id=manifest["dataset_id"],
        manifest_sha256=_save(private / "manifest.json", manifest),
        distribution=distribution,
        grouping_evidence="test fixture metadata",
        custodian=evidence,
        exposed_before_freeze=False,
    )
    records, conditions = [], {}
    validation_reference = {"intent": "damaged"}
    case_sha, reference_sha = "d" * 64, formal.digest(validation_reference)
    for group, mode, strategy in (
        ("A", "single", "economy"),
        ("A", "single", "analysis"),
        ("A", "single", "routed"),
        ("B", "single", "routed"),
        ("B", "multi", "routed"),
    ):
        ident = f"{mode}-{strategy}"
        path = root / f"{ident}.json"
        report = {
            "name": ident,
            "id": ident,
            "status": "COMPLETED",
            "config": {
                "dataset_id": "v0.2",
                "split": "validation",
                "source": "fixture",
                "mode": mode,
                "strategy": strategy,
                "code_digest": "a" * 64,
                "metrics_version": "metrics",
                "scorer_version": "scorer",
                "model_snapshot": None,
                "input_digest": formal.digest({"V1": case_sha}),
                "reference_digest": formal.digest({"V1": reference_sha}),
            },
            "counts": {"planned": 1, "started": 1, "finished": 1, "not_run": 0},
            "metrics": [],
            "gate": {},
            "results": [
                {
                    "case_id": "V1",
                    "repetition": 1,
                    "status": "NEEDS_REVIEW",
                    "started_at": "observed",
                    "finished_at": "observed",
                    "reviews": [],
                    "checks": [],
                    "expected": validation_reference,
                    "quota": {"case_hash": case_sha},
                }
            ],
        }
        conditions[f"{mode}/{strategy}"] = formal.digest(report["config"])
        records.append({"group": group, "path": str(path), "sha256": _save(path, report)})
    plan = ValidationPlan(
        dataset_id="v0.2",
        cases={"V1": {"case_sha256": case_sha, "reference_sha256": reference_sha}},
        repetitions=1,
        config_sha256=conditions,
        policies_digest="b" * 64,
        retrieval_digest="c" * 64,
    )
    validation = Validation(
        plan_sha256=formal._sha(plan),
        reports=records,
        selected_experiment_id="single-routed",
        rationale="Synthetic configuration selection for a test only",
        selection=evidence,
    )
    return SimpleNamespace(
        store=store,
        governance=governance,
        private=private,
        candidate=candidate,
        validation=validation,
        plan=plan,
        human=evidence,
    )


def _record(campaign, ident="test-campaign", candidate=None):
    candidate = campaign.store.register(ident, candidate or campaign.candidate)
    campaign.store.plan(ident, campaign.plan)
    validation = campaign.store.validation(ident, campaign.validation)
    draft = FreezeDraft(
        candidate_sha256=candidate["candidate_sha256"],
        validation_sha256=validation["validation_sha256"],
        code_digest="a" * 64,
        model_snapshot_digest=None,
        policies_digest="b" * 64,
        retrieval_digest="c" * 64,
        scorer_version="scorer",
        metrics_version="metrics",
        repetitions=1,
        max_calls=10,
        budget_usd=0,
        gate_policy={
            "id": "fixture-gate",
            "version": "1",
            "name": "test",
            "source": "test",
            "thresholds": {
                "min_task_success": 1,
                "min_independent_cases": 1,
                "require_live": False,
            },
        },
    )
    confirmation = FreezeConfirmation(
        draft_sha256=formal._sha(draft), decision="CONFIRMED", human=campaign.human
    )
    return draft, confirmation


def _freeze(campaign):
    return campaign.store.freeze("test-campaign", *_record(campaign))


def _unlock(campaign, frozen, *, directory=None):
    return Unlock(
        freeze_sha256=frozen["freeze_sha256"],
        directory=str(directory or campaign.private),
        handoff=campaign.human,
    )


def _journal(campaign):
    path = campaign.governance / "test-campaign/exposure.jsonl"
    return [json_data(line) for line in path.read_bytes().splitlines()]


def test_preflight_does_not_open_candidate_and_unlock_journals_before_read(campaign, monkeypatch):
    frozen = _freeze(campaign)
    real_inspect = formal._inspect_package
    inspected = []

    def inspect(*args):
        assert [event["kind"] for event in _journal(campaign)] == ["OPENING"]
        inspected.append(True)
        return real_inspect(*args)

    monkeypatch.setattr(formal, "_inspect_package", inspect)
    file = campaign.private / "manifest.json"
    displaced = file.with_suffix(".away")
    file.rename(displaced)
    preflight = campaign.store.preflight("test-campaign")
    assert preflight["status"] == "FROZEN" and not preflight["execution_available"]
    assert not inspected and not (campaign.governance / "test-campaign/exposure.jsonl").exists()
    displaced.rename(file)
    unlocked = campaign.store.unlock("test-campaign", _unlock(campaign, frozen))
    assert unlocked["status"] == "UNLOCKED" and not unlocked["execution_available"]
    assert inspected == [True]
    assert [event["kind"] for event in _journal(campaign)] == ["OPENING", "CASES_READ", "UNLOCKED"]
    assert not any(str(campaign.private) in str(event) for event in _journal(campaign))
    with pytest.raises(BusinessError) as error:
        campaign.store.unlock("test-campaign", _unlock(campaign, frozen))
    assert error.value.code == "FORMAL_UNLOCK_BLOCKED"


def test_freeze_is_immutable_and_preflight_never_reopens_external_reports(campaign, monkeypatch):
    draft, confirmation = _record(campaign)
    replacement = Path(campaign.validation.reports[0].path)
    replacement.write_bytes((campaign.private / "cases.json").read_bytes())

    def deny_external_read(*_args, **_kwargs):
        pytest.fail("Preflight or freeze opened external data")

    monkeypatch.setattr(formal, "read_path", deny_external_read)
    assert campaign.store.preflight("test-campaign")["status"] == "REGISTERED"
    campaign.store.freeze("test-campaign", draft, confirmation)
    original = (campaign.governance / "test-campaign/freeze.json").read_bytes()
    with pytest.raises(BusinessError) as error:
        campaign.store.freeze("test-campaign", draft, confirmation)
    assert error.value.code == "FORMAL_RECORD_EXISTS"
    assert (campaign.governance / "test-campaign/freeze.json").read_bytes() == original
    assert campaign.store.preflight("test-campaign")["status"] == "FROZEN"
    snapshot = (campaign.governance / "test-campaign/validation.json").read_text()
    assert str(replacement) not in snapshot


@pytest.mark.parametrize("failure", ["checksum", "package-symlink", "file-symlink", "interrupted"])
def test_failed_open_stays_uncertain_and_cannot_reopen(campaign, monkeypatch, failure):
    frozen = _freeze(campaign)
    target = campaign.private
    if failure == "checksum":
        (target / "cases.json").write_bytes(b"[]")
    if failure == "package-symlink":
        target = campaign.private.with_name("linked-package")
        target.symlink_to(campaign.private, target_is_directory=True)
    if failure == "file-symlink":
        original = target / "cases.json"
        real = original.rename(target / "cases-real.json")
        original.symlink_to(real)
    if failure == "interrupted":

        def interrupted(*_):
            raise KeyboardInterrupt

        monkeypatch.setattr(formal, "_inspect_package", interrupted)
        with pytest.raises(KeyboardInterrupt):
            campaign.store.unlock("test-campaign", _unlock(campaign, frozen))
    else:
        with pytest.raises(BusinessError) as error:
            campaign.store.unlock("test-campaign", _unlock(campaign, frozen, directory=target))
        assert error.value.code == "FORMAL_PACKAGE_OPEN_FAILED"
    assert campaign.store.preflight("test-campaign")["status"] == "EXPOSURE_UNCERTAIN"
    assert _journal(campaign)[0]["kind"] == "OPENING"
    with pytest.raises(BusinessError) as error:
        campaign.store.unlock("test-campaign", _unlock(campaign, frozen))
    assert error.value.code == "FORMAL_UNLOCK_BLOCKED"


def test_lock_serializes_two_unlocks(campaign, monkeypatch):
    frozen = _freeze(campaign)
    opened = Event()
    release = Event()
    real_inspect = formal._inspect_package

    def inspect(*args):
        opened.set()
        assert release.wait(5)
        return real_inspect(*args)

    monkeypatch.setattr(formal, "_inspect_package", inspect)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(campaign.store.unlock, "test-campaign", _unlock(campaign, frozen))
        try:
            assert opened.wait(5)
            second = pool.submit(campaign.store.unlock, "test-campaign", _unlock(campaign, frozen))
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "UNLOCKED"
        with pytest.raises(BusinessError) as error:
            second.result(timeout=5)
        assert error.value.code == "FORMAL_UNLOCK_BLOCKED"
    assert [item["kind"] for item in _journal(campaign)] == ["OPENING", "CASES_READ", "UNLOCKED"]


def test_exposed_data_and_wrong_campaign_path_are_blocked(campaign):
    with pytest.raises(BusinessError) as error:
        campaign.store.register("old", campaign.candidate.model_copy(update={"dataset_id": "v0.2"}))
    assert error.value.code == "FORMAL_DATASET_EXPOSED"
    with pytest.raises(ValueError):
        campaign.store.register("../escape", campaign.candidate)
    draft, confirmation = _record(campaign)
    campaign.store.record_exposure("test-campaign", campaign.human)
    assert campaign.store.preflight("test-campaign")["status"] == "EXPOSED_BEFORE_FREEZE"
    with pytest.raises(BusinessError) as error:
        campaign.store.freeze("test-campaign", draft, confirmation)
    assert error.value.code == "FORMAL_DATASET_EXPOSED"


def test_fixture_validation_cannot_establish_formal_selection(campaign):
    candidate = campaign.candidate.model_copy(update={"purpose": "formal"})
    campaign.store.register("test-campaign", candidate)
    campaign.store.plan("test-campaign", campaign.plan)
    with pytest.raises(BusinessError) as error:
        campaign.store.validation("test-campaign", campaign.validation)
    assert error.value.code == "FORMAL_VALIDATION_MISMATCH"
    assert campaign.store.preflight("test-campaign")["validation_sha256"] is None


@pytest.mark.parametrize("first_failed", [False, True])
def test_repackaging_previously_read_cases_under_another_manifest_is_blocked(
    campaign, first_failed
):
    frozen = _freeze(campaign)
    refs_path = campaign.private / "references.json"
    original_refs = refs_path.read_bytes()
    if first_failed:
        refs_path.write_bytes(b"{}")
        with pytest.raises(BusinessError):
            campaign.store.unlock("test-campaign", _unlock(campaign, frozen))
        assert [item["kind"] for item in _journal(campaign)] == [
            "OPENING",
            "CASES_READ",
            "EXPOSURE_UNCERTAIN",
        ]
        refs_path.write_bytes(original_refs)
    else:
        campaign.store.unlock("test-campaign", _unlock(campaign, frozen))
    renamed = campaign.private.with_name("renamed-package")
    renamed.mkdir()
    manifest = json_data((campaign.private / "manifest.json").read_bytes())
    cases = json_data((campaign.private / "cases.json").read_bytes())
    manifest["dataset_id"] = "renamed-package"
    references = json_data((campaign.private / "references.json").read_bytes())
    new_refs = {}
    for i, case in enumerate(cases):
        new_id = f"RENAMED-{i}"
        new_refs[new_id] = references[case["id"]]
        case["id"] = new_id
        case["events"][0]["id"] = "different-label"
        if i:
            case["message"] = f"different unique phrase {i}"
    manifest["cases_sha256"] = _save(renamed / "cases.json", list(reversed(cases)))
    manifest["references_sha256"] = _save(renamed / "references.json", new_refs)
    renamed_candidate = campaign.candidate.model_copy(
        update={
            "dataset_id": "renamed-package",
            "manifest_sha256": _save(renamed / "manifest.json", manifest),
        }
    )
    draft, confirmation = _record(campaign, "second", renamed_candidate)
    second = campaign.store.freeze("second", draft, confirmation)
    with pytest.raises(BusinessError) as error:
        campaign.store.unlock("second", _unlock(campaign, second, directory=renamed))
    assert error.value.code == "FORMAL_PACKAGE_OPEN_FAILED"
    assert campaign.store.preflight("second")["status"] == "EXPOSURE_UNCERTAIN"


def test_cli_check_draft_returns_confirmation_digest_without_writing(campaign, monkeypatch, capsys):
    from aftercare.evaluation.formal.__main__ import main

    draft, confirmation = _record(campaign)
    path = campaign.private.with_name("draft.json")
    _save(path, draft.model_dump(mode="json"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "formal",
            "--root",
            str(campaign.governance),
            "check-draft",
            "--campaign",
            "test-campaign",
            "--record",
            str(path),
        ],
    )
    assert main() == 0
    response = json_data(capsys.readouterr().out)
    assert response["draft_sha256"] == confirmation.draft_sha256
    assert response["selected"]["id"] == "single-routed"
    assert not (campaign.governance / "test-campaign/freeze.json").exists()


def test_report_needs_preregistered_full_input_and_exact_configs(campaign):
    campaign.store.register("test-campaign", campaign.candidate)
    with pytest.raises(BusinessError) as error:
        campaign.store.validation("test-campaign", campaign.validation)
    assert error.value.code == "FORMAL_VALIDATION_PLAN_REQUIRED"
    plan = campaign.plan.model_copy(
        update={"cases": {**campaign.plan.cases, "V2": campaign.plan.cases["V1"]}}
    )
    campaign.store.plan("test-campaign", plan)
    validation = campaign.validation.model_copy(update={"plan_sha256": formal._sha(plan)})
    with pytest.raises(BusinessError) as error:
        campaign.store.validation("test-campaign", validation)
    assert error.value.code == "FORMAL_VALIDATION_MISMATCH"


def test_self_asserted_live_reports_and_actor_do_not_authorize_formal_freeze(campaign):
    candidate = campaign.candidate.model_copy(update={"purpose": "formal"})
    conditions, entries = {}, []
    for report in campaign.validation.reports:
        path = Path(report.path)
        raw = json_data(path.read_bytes())
        raw["config"].update(source="live", model_snapshot={"models": ["claimed"]})
        raw["results"][0]["actual"] = {"observed_source": "live_provider", "model_attempts": [{}]}
        config = raw["config"]
        conditions[f"{config['mode']}/{config['strategy']}"] = formal.digest(config)
        entries.append(report.model_copy(update={"sha256": _save(path, raw)}))
    campaign.plan = campaign.plan.model_copy(update={"config_sha256": conditions})
    campaign.validation = campaign.validation.model_copy(
        update={"reports": entries, "plan_sha256": formal._sha(campaign.plan)}
    )
    draft, confirmation = _record(campaign, candidate=candidate)
    state = campaign.store.preflight("test-campaign")
    assert state["source_verification"] == "UNVERIFIED_LOCAL_REPORT"
    assert state["human_identity"] == "UNAUTHENTICATED_DECLARATION"
    with pytest.raises(BusinessError) as error:
        campaign.store.freeze("test-campaign", draft, confirmation)
    assert error.value.code == "FORMAL_VALIDATION_SOURCE_UNVERIFIED"
    assert not (campaign.governance / "test-campaign/freeze.json").exists()


@pytest.mark.parametrize("changed", ["config", "quota", "expected"])
def test_validation_checks_frozen_inputs_even_with_a_matching_report_checksum(campaign, changed):
    campaign.store.register("test-campaign", campaign.candidate)
    campaign.store.plan("test-campaign", campaign.plan)
    original = campaign.validation.reports[0]
    path = Path(original.path)
    report = json_data(path.read_bytes())
    if changed == "config":
        report["config"]["concurrency"] = 2
    elif changed == "quota":
        report["results"][0]["quota"]["case_hash"] = "e" * 64
    else:
        report["results"][0]["expected"]["intent"] = "wrong_item"
    replacement = original.model_copy(update={"sha256": _save(path, report)})
    validation = campaign.validation.model_copy(
        update={"reports": [replacement, *campaign.validation.reports[1:]]}
    )
    with pytest.raises(BusinessError) as error:
        campaign.store.validation("test-campaign", validation)
    assert error.value.code == "FORMAL_VALIDATION_MISMATCH"
