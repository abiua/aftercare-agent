"""Public register transitions with synthetic native evidence, never real holdout."""

import pytest
from test_formal_handoff import CAP, OPERATOR, _prepared

from aftercare.business.errors import BusinessError
from aftercare.evaluation.formal import register as formal
from aftercare.evaluation.formal.contracts import (
    FreezeConfirmation,
    FreezeQuota,
    parse_freeze_draft,
)


@pytest.mark.parametrize("interleaved", [False, True])
@pytest.mark.parametrize("denial", ["missing-cap", "wrong-sha", "small-cap", "valid"])
def test_public_freeze_unlock_and_reopen_are_bound_before_candidate_read(tmp_path, monkeypatch, denial, interleaved):
    fixture = _prepared(tmp_path.resolve(), frozen_record=False, interleaved=interleaved)
    register, campaign = fixture.register, fixture.campaign_id
    draft = parse_freeze_draft(fixture.frozen["draft"])
    confirmation = FreezeConfirmation.model_validate(fixture.frozen["confirmation"])
    calls = []
    original = formal.load_private_package

    def tracked(*args, **kwargs):
        calls.append("read")
        return original(*args, **kwargs)

    monkeypatch.setattr(formal, "load_private_package", tracked)
    preview = register.preview_freeze(campaign, draft, confirmation)
    assert preview == register.preview_freeze(campaign, draft, confirmation)
    assert preview["prospective_freeze_sha256"] == fixture.request.freeze_sha256
    assert preview["deployment_authorization"] == "NOT_CHECKED"
    assert not calls and not fixture.journal.exists()
    kwargs = {"execution_cap": CAP, "expected_freeze_sha256": fixture.request.freeze_sha256}
    if denial == "missing-cap":
        kwargs.pop("execution_cap")
    elif denial == "wrong-sha":
        kwargs["expected_freeze_sha256"] = "0" * 64
    elif denial == "small-cap":
        kwargs["execution_cap"] = FreezeQuota(calls=1, budget_microusd=1)
    if denial != "valid":
        with pytest.raises(BusinessError):
            register.freeze(campaign, draft, confirmation, **kwargs)
        assert not (register.root / campaign / "freeze.json").exists()
        assert not calls and not fixture.journal.exists()
        return
    frozen = register.freeze(campaign, draft, confirmation, **kwargs)
    assert frozen["freeze_sha256"] == preview["prospective_freeze_sha256"]
    assert not calls
    with pytest.raises(BusinessError):
        register.unlock(campaign, fixture.request, execution_cap=CAP, expected_freeze_sha256="0" * 64)
    assert not calls and not fixture.journal.exists()
    register.unlock(campaign, fixture.request, **kwargs)
    assert calls == ["read"]
    state = register.preflight(campaign)
    assert state["execution_available"] is True
    assert state["deployment_authorization"] == "NOT_CHECKED"
    handoff = register.open_for_execution(campaign, operator=OPERATOR, **kwargs)
    assert handoff.freeze_sha256 == frozen["freeze_sha256"]
    assert handoff.draft.model_dump(mode="json") == draft.model_dump(mode="json")
    assert [event["kind"] for event in fixture.events()] == [
        "OPENING", "CASES_READ", "UNLOCKED", "REOPENING", "CASES_READ", "REOPENED",
    ]


@pytest.mark.parametrize("denial", ["wrong-deployment", "revoked-tester", "valid"])
def test_orchestrated_freeze_and_unlock_check_deployment_and_actor_first(tmp_path, monkeypatch, denial):
    from aftercare.evaluation.formal import orchestrator

    fixture = _prepared(tmp_path.resolve(), frozen_record=False)
    register, campaign = fixture.register, fixture.campaign_id
    draft = parse_freeze_draft(fixture.frozen["draft"])
    confirmation = FreezeConfirmation.model_validate(fixture.frozen["confirmation"])
    checked = []

    def deployment(campaign_id, freeze_sha):
        assert (campaign_id, freeze_sha) == (campaign, fixture.request.freeze_sha256)
        checked.append("deployment")
        if denial == "wrong-deployment":
            raise BusinessError("FORMAL_EXECUTION_CAP_MISMATCH", "controlled mismatch", 409)
        return CAP

    def tester():
        checked.append("tester")
        if denial == "revoked-tester":
            raise BusinessError("FORMAL_TESTER_UNAVAILABLE", "controlled revocation", 403)

    monkeypatch.setattr(orchestrator, "_deployment", deployment)
    monkeypatch.setattr(orchestrator, "_require_tester", tester)
    if denial != "valid":
        with pytest.raises(BusinessError):
            orchestrator.freeze_campaign(register, campaign, draft, confirmation)
        assert not fixture.journal.exists()
        assert not (register.root / campaign / "freeze.json").exists()
        assert checked == (["deployment"] if denial == "wrong-deployment" else ["deployment", "tester"])
        return
    orchestrator.freeze_campaign(register, campaign, draft, confirmation)
    assert checked == ["deployment", "tester"] and not fixture.journal.exists()
    # The same identity gate is repeated before unlock can touch candidate contents.
    denial = "revoked-tester"
    with pytest.raises(BusinessError):
        orchestrator.unlock_campaign(register, campaign, fixture.request)
    assert not fixture.journal.exists()
    denial = "valid"
    orchestrator.unlock_campaign(register, campaign, fixture.request)
    assert register.preflight(campaign)["status"] == "UNLOCKED"
