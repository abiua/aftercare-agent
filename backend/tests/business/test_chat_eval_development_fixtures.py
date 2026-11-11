"""Verify development inputs and policy constraints using disposable SQLite only."""

import pytest
from chat_eval_development_fixtures import development_source, load_development_cases
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.models import Approval, Execution
from aftercare.business.rules import digest
from aftercare.evaluation.chat.runner import _source
from aftercare.investigation.input import build_input
from aftercare.investigation.routing import route_input

PACK = load_development_cases()


@pytest.mark.parametrize("case", PACK["cases"], ids=lambda case: case["id"])
def test_development_chat_source_is_frozen_and_no_new_financial_action(tmp_path, case):
    with development_source(tmp_path, case["id"]) as source:
        frozen = _source(source)
        scope = frozen.scope
        context = scope.frozen_context
        line = context["order"]["items"][0]
        assert PACK["independent_human_judgments"] is False
        assert line["quantity"] == case["quantity"]
        assert line["paid_cents"] == case["quantity"] * case["unit_paid_cents"]
        assert line["package_id"] == case["package_id"]
        assert [message["text"] for message in context["messages"]] == [case["customer_message"]]
        assert [item[0]["size"] for item in frozen.images] == [1954558, 2574993]
        assert {row["kind"] for row in context["materials"]} == {"ITEM", "PARCEL"}
        assert len(context["reviews"]) == 1 and context["reviews"][0]["effective"]
        assert set(context["reviews"][0]["material_ids"]) == {
            row["id"] for row in context["materials"]
        }
        assert not context["observations"]
        assert source.provenance.endswith(digest(PACK))
        assert set(PACK["expected_applicable_policy_ids"]) <= {
            f"{doc['id']}:{doc['version']}" for doc in context["policies"]["documents"]
        }
        if "additional_line" in case:
            assert scope.line_ids == ("L01", "L02")
            other = context["order"]["items"][1]
            assert other["paid_cents"] == 7200 and other["package_id"] == "TEST-PARCEL-B17"
            route = route_input(build_input(scope))
            assert route.path == case["expected_default_route"] == "MULTI"
            assert "MULTIPLE_CLAIMED_LINES" in route.complexity_features
            assert case["local_recheck_evidence"] == "NOT_RUN"
        with Session(source.source.engine) as db:
            count = int("history" in case)
            assert db.scalar(select(func.count()).select_from(Execution)) == count
            assert db.scalar(select(func.count()).select_from(Approval)) == count
            if count:
                prior = db.get_one(Execution, case["history"]["execution_id"])
                assert prior.status == "SUCCEEDED"
                assert prior.receipt["provenance"] == "SIMULATED"
                assert prior.receipt["package_id"] != line["package_id"]
                assert prior.receipt["quantity"] == 1
                assert line["refunded_cents"] == prior.amount_cents == 6000
                assert scope.business_versions["ledger"] == digest(
                    [
                        [
                            prior.id,
                            prior.action,
                            prior.status,
                            prior.amount_cents,
                            prior.lines,
                            prior.receipt,
                        ]
                    ]
                )
        # A damage case cannot read another case's refund details from its scoped ledger.
        assert context["ledger"] == []
        if count:
            with pytest.raises(BusinessError) as error:
                source.source.validate_draft(
                    scope.case_id,
                    source.actor,
                    expected_version=scope.business_versions["case"],
                    action="REFUND",
                    lines=[{"line_id": "L01", "amount_cents": 6000}],
                    policy_ids=["POL06", "POL14"],
                )
            assert error.value.code == "DUPLICATE_MATTER"
        if case["id"] == "DAMAGE-ORDINARY":
            plan = source.source.validate_draft(
                scope.case_id,
                source.actor,
                expected_version=scope.business_versions["case"],
                action="REFUND",
                lines=[{"line_id": "L01", "amount_cents": 6000}],
                policy_ids=["POL06", "POL14"],
            )
            assert plan["amount_cents"] == 6000
