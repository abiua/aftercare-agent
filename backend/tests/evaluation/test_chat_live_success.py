import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def success(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("verify_chat_live_success")


def _record(kind, ident, **data):
    return {"ref": {"source_kind": kind, "source_id": ident}, "data": data}


def test_ordinary_accept_requires_exact_requested_amount_and_actual_policy_receipt(success):
    option = {
        "id": "refund",
        "action": "REFUND",
        "lines": [{"line_id": "L01", "amount_cents": 4900}],
        "policy_ids": ["POL01"],
        "evidence_ids": ["L01", "POL01:1.0"],
    }
    snapshot = {
        "candidate": {"options": [option], "unresolved": []},
        "checks": {"refund": {"status": "PASSED"}},
    }
    trace = {
        "receipts": [
            {
                "role": "strong_single",
                "records": [
                    _record("ORDER_LINE", "L01", paid_cents=4900, refunded_cents=0),
                ],
            }
        ]
    }
    assert not success._ordinary_ready(trace, snapshot)
    trace["receipts"][0]["records"].append(
        _record("POLICY", "POL01:1.0", id="POL01", version="1.0")
    )
    assert success._ordinary_ready(trace, snapshot)
    option["lines"][0]["amount_cents"] = 4899
    assert not success._ordinary_ready(trace, snapshot)


def test_complex_scope_distinguishes_customer_conflict_from_business_mapping(success):
    claim = {
        "id": "fact/package",
        "subject": "L02",
        "predicate": "package",
        "value": "TEST-PARCEL-B17",
        "state": "CONTRADICTED",
        "conflicting_refs": ["L02"],
    }
    trace = {
        "inputs": [
            {"role": "fact", "phase_id": "fact", "upstream_claim_ids": []},
            {"role": "policy", "phase_id": "policy", "upstream_claim_ids": []},
            {"role": "verifier", "phase_id": "verify-0", "upstream_claim_ids": ["fact/package"]},
        ],
        "receipts": [
            {
                "role": "fact",
                "records": [
                    _record("ORDER_LINE", "L01", package_id="TEST-PARCEL-A17"),
                    _record("ORDER_LINE", "L02", package_id="TEST-PARCEL-B17"),
                ],
            },
            {"role": "policy", "records": [_record("POLICY", "POL01:1.0")]},
        ],
        "claims": [{"role": "fact", "phase_id": "fact", "claims": [claim]}],
        "assessments": [
            {
                "recheck_count": 0,
                "assessments": [
                    {
                        "claim_id": "fact/package",
                        "verdict": "CONTRADICTED",
                        "evidence_refs": ["L02"],
                    },
                ],
            }
        ],
        "rechecks": [],
    }
    result = success._complex_outcome(trace, customer_statement_bound=True)
    assert result["native_package_mapping_read"] and result["independent_fact_policy_and_verifier"]
    assert not result["model_identified_package_conflict"]
    claim["value"] = "TEST-PARCEL-A17"
    assert not success._complex_outcome(trace, customer_statement_bound=False)[
        "model_identified_package_conflict"
    ]
    result = success._complex_outcome(trace, customer_statement_bound=True)
    assert result["model_identified_package_conflict"]
    assert result["targeted_recheck"] == "NOT_OBSERVED"


def test_ordinary_attempts_use_disjoint_targets_and_keep_business_scope(success):
    prior = success.driver.STATE
    with success._target("ordinary") as first:
        assert success.driver.PORT == 5204
        first_subnets = set(success.driver.SUBNETS.values())
    with success._target("ordinary", 2) as second:
        assert second != first and second.name.endswith("ordinary-r2.json")
        assert success.offline_demo.STATE == success.driver.STATE == second
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5206, 58368)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.96/28", "10.253.252.112/28"}
        second_subnets = set(success.driver.SUBNETS.values())
        assert first_subnets.isdisjoint(second_subnets)
    with success._target("ordinary", 3) as third:
        assert third not in {first, second} and third.name.endswith("ordinary-r3.json")
        assert success.offline_demo.STATE == success.driver.STATE == third
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5207, 58369)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.128/28", "10.253.252.144/28"}
        third_subnets = set(success.driver.SUBNETS.values())
        assert (first_subnets | second_subnets).isdisjoint(third_subnets)
    with success._target("ordinary", 4) as fourth:
        assert fourth not in {first, second, third} and fourth.name.endswith("ordinary-r4.json")
        assert success.offline_demo.STATE == success.driver.STATE == fourth
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5208, 58370)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.160/28", "10.253.252.176/28"}
        assert (first_subnets | second_subnets | third_subnets).isdisjoint(
            success.driver.SUBNETS.values()
        )
        assert success.SCENARIOS["ordinary"]["order"] == "O9902"
        assert success.SCENARIOS["ordinary"]["lines"] == ["L01"]
        fourth_subnets = set(success.driver.SUBNETS.values())
    with success._target("ordinary", 5) as fifth:
        assert fifth not in {first, second, third, fourth} and fifth.name.endswith(
            "ordinary-r5.json"
        )
        assert success.offline_demo.STATE == success.driver.STATE == fifth
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5209, 58371)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.192/28", "10.253.252.208/28"}
        assert (first_subnets | second_subnets | third_subnets | fourth_subnets).isdisjoint(
            success.driver.SUBNETS.values()
        )
        fifth_subnets = set(success.driver.SUBNETS.values())
    with success._target("ordinary", 6) as sixth:
        assert sixth not in {first, second, third, fourth, fifth}
        assert sixth.name.endswith("ordinary-r6.json")
        assert success.offline_demo.STATE == success.driver.STATE == sixth
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5210, 58372)
        assert set(success.driver.SUBNETS.values()) == {"10.253.252.224/28", "10.253.252.240/28"}
        assert (
            first_subnets | second_subnets | third_subnets | fourth_subnets | fifth_subnets
        ).isdisjoint(success.driver.SUBNETS.values())
        sixth_subnets = set(success.driver.SUBNETS.values())
    with success._target("ordinary", 7) as seventh:
        assert seventh not in {first, second, third, fourth, fifth, sixth}
        assert seventh.name.endswith("ordinary-r7.json")
        assert success.offline_demo.STATE == success.driver.STATE == seventh
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5211, 58373)
        assert set(success.driver.SUBNETS.values()) == {"10.253.253.0/28", "10.253.253.16/28"}
        assert (
            first_subnets
            | second_subnets
            | third_subnets
            | fourth_subnets
            | fifth_subnets
            | sixth_subnets
        ).isdisjoint(success.driver.SUBNETS.values())
    with success._target("complex"):
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5205, 58367)
    assert success.driver.STATE == prior
    with (
        pytest.raises(success.VerificationError, match="LIVE_SUCCESS_ATTEMPT_UNSUPPORTED"),
        success._target("complex", 7),
    ):
        pytest.fail("complex retry should not be admitted")


def test_v3_registry_budget_is_derived_without_changing_historical_or_issue_caps(success):
    driver = success.driver
    spec = driver.models.ModelSpec.model_validate(
        {
            "alias": "economy",
            "provider": "openai",
            "model": "gpt-6.1-terra",
            "endpoint": "https://api.openai.com/v1",
            "interface": "responses",
            "api_key_env": "AFTERCARE_OPENAI_KEY",
            "context_tokens": 32000,
            "output_tokens": 900,
            "input_usd_per_million": "1",
            "output_usd_per_million": "5",
            "cache_read_usd_per_million": "0.10",
            "cache_write_usd_per_million": "1.25",
            "cache_write_ttl": "30m",
            "price_date": "2026-11-11",
            "source": "offline fixture",
            "data_scopes": ["customer_attachment"],
        }
    )
    _, _, _, photos = driver._inputs()
    historical = driver._budgets(spec, photos)
    updated = driver._budgets(spec, photos, wire_protocol=success.PROTOCOL)
    assert historical["text"]["output_tokens"] == 1800
    assert historical["text"]["maximum_estimated_usd"] == "0.40"
    assert updated["vision"] == historical["vision"]
    assert updated["text"]["output_tokens"] == 4096
    assert updated["text"]["maximum_estimated_usd"] == "0.60"
    assert updated["text"]["maximum_posts"] == 8
    assert updated["text"]["reserve_per_post_microusd"] == 51_200
    assert updated["text"]["reserve_at_post_limit_microusd"] == 409_600
    assert updated["native_shared_issue"] == historical["native_shared_issue"]
    plan = {"budget": updated, "wire_protocol": success.PROTOCOL}
    registry = driver._phase_registry(spec, plan, "TEXT", photos)
    assert registry["models"][0]["output_tokens"] == 4096
    assert registry["max_run_usd"] == "0.60"


def test_r8_and_complex_r2_have_independent_state_and_networks(success):
    with success._target("ordinary", 8) as ordinary:
        assert ordinary.name.endswith("ordinary-r8.json")
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5212, 58374)
        assert set(success.driver.SUBNETS.values()) == {"10.253.253.32/28", "10.253.253.48/28"}
    with success._target("complex", 2) as complex_state:
        assert complex_state.name.endswith("complex-r2.json")
        assert success.offline_demo.STATE == success.driver.STATE == complex_state
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5213, 58375)
        assert set(success.driver.SUBNETS.values()) == {"10.253.253.64/28", "10.253.253.80/28"}
        assert success.SCENARIOS["complex"]["lines"] == ["L01", "L02"]


def test_typed_control_only_submits_item_observation(success):
    spec = success.CONTROL_OBSERVATION
    item = {"kind": "ITEM", "material_id": "m-item"}
    packaging = {"kind": "PACKAGE", "material_id": "m-package"}
    bound, observation = success.driver._typed_control_review(spec, [packaging, item])
    assert bound == item
    assert observation == {
        "material_id": "m-item",
        "finding": "VISIBLE_DAMAGE",
        "line_id": "L01",
        "affected_quantity": 1,
    }
    for change in ({"line_id": "L02"}, {"affected_quantity": True}, {"affected_quantity": 2}):
        with pytest.raises(success.VerificationError, match="TYPED_CONTROL_SCOPE_CHANGED"):
            success.driver._typed_control_review({**spec, **change}, [item])
    with pytest.raises(success.VerificationError, match="TYPED_CONTROL_ITEM_LINK_AMBIGUOUS"):
        success.driver._typed_control_review(spec, [item, item])


def test_typed_control_frozen_binding_does_not_substitute_for_actual_tool_read(success):
    control = {"review_id": "r1", "attachment_id": "a1", "original_sha256": "original"}
    observation = {
        "material_id": "m1",
        "finding": "VISIBLE_DAMAGE",
        "source": "HUMAN_MATERIAL_REVIEW",
    }
    readback = {"observations": [observation], "materials_version": 2, "message_id": "msg"}
    report = {"material_review_control": control, "material_review_control_readback": readback}
    expected = {**observation, "material_sha256": "original"}
    original = {"attachment_id": "a1", "message_id": "msg", "sha256": "original"}
    review = {
        "effective": True,
        "id": "r1",
        "materials_version": 2,
        "observations": [{**expected, "original_attachments": [original]}],
    }
    snapshot = {"input": {"frozen_context": {"reviews": [review]}}}
    record = {
        "ref": {"source_id": "r1", "source_kind": "REVIEW"},
        "data": {"observations": [expected]},
    }
    assert not success._verify_typed_control(snapshot, {"receipts": []}, report)
    trace = {"receipts": [{"role": "strong_single", "records": [record]}]}
    assert success._verify_typed_control(snapshot, trace, report)
    original["attachment_id"] = "another-attachment"
    with pytest.raises(success.VerificationError, match="TYPED_CONTROL_FROZEN_BINDING_CHANGED"):
        success._verify_typed_control(snapshot, trace, report)


def test_ordinary_r9_has_own_state_and_networks(success):
    with success._target("ordinary", 9) as ninth:
        assert ninth.name.endswith("ordinary-r9.json")
        assert success.offline_demo.STATE == success.driver.STATE == ninth
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5214, 58376)
        assert set(success.driver.SUBNETS.values()) == {"10.253.253.96/28", "10.253.253.112/28"}
    with (
        pytest.raises(success.VerificationError, match="LIVE_SUCCESS_ATTEMPT_UNSUPPORTED"),
        success._target("complex", 9),
    ):
        pytest.fail("ordinary retry must not affect complex targets")


def test_ordinary_r10_uses_fresh_state_and_networks(success):
    with success._target("ordinary", 10) as tenth:
        assert tenth.name.endswith("ordinary-r10.json")
        assert success.offline_demo.STATE == success.driver.STATE == tenth
        assert (success.driver.PORT, success.driver.OBJECT_PORT) == (5215, 58377)
        assert set(success.driver.SUBNETS.values()) == {"10.253.253.128/28", "10.253.253.144/28"}
