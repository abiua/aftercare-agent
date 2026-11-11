"""Controlled private journal handoff. No real holdout, database or model traffic."""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_formal_package import DISTRIBUTION, _put, _synthetic, _write
from test_formal_source_verification import (
    STAMP,
    _draft_data,
    _fixtures,
    _snapshot,
    _with_controls,
)

from aftercare.business.errors import BusinessError
from aftercare.evaluation.formal import register as formal
from aftercare.evaluation.formal.contracts import (
    Candidate,
    FreezeConfirmation,
    FreezeQuota,
    HumanRecord,
    Unlock,
    parse_freeze_draft,
)
from aftercare.evaluation.formal.files import append_event, json_bytes, json_data, write_once

CAP = FreezeQuota(calls=12, budget_microusd=1200)
OPERATOR = HumanRecord(actor="second controlled tester", at=STAMP, source="fixture only")


@dataclass
class HandoffFixture:
    register: formal.CampaignRegister
    package_path: Path
    request: Unlock
    campaign_id: str
    frozen: dict

    @property
    def journal(self):
        return self.register.root / self.campaign_id / "exposure.jsonl"

    def events(self):
        return [json_data(line) for line in self.journal.read_bytes().splitlines()]

    def unlock(self):
        return self.register._prepare_unlock(self.campaign_id, self.request, execution_cap=CAP)

    def reopen(self, *, expected=None, cap=CAP):
        return self.register._prepare_execution(
            self.campaign_id,
            expected_freeze_sha256=self.request.freeze_sha256 if expected is None else expected,
            execution_cap=cap,
            operator=OPERATOR,
        )


def _prepared(root, *, campaign_id="one", changes=None, frozen_record=True, interleaved=False, source_fixture=None):
    fixture = source_fixture if source_fixture is not None else _with_controls(_fixtures())
    snapshot = _snapshot(fixture)
    package_path = root / f"{campaign_id}-candidate"
    package_path.mkdir(mode=0o700)
    cases, refs = _synthetic()
    if changes is not None:
        changes(cases, refs)
    dataset_id = f"synthetic-{campaign_id}"
    manifest_sha = _put(package_path, cases, refs, manifest_overrides={"dataset_id": dataset_id})
    custodian = HumanRecord(actor="controlled custodian", at=STAMP, source="fixture only")
    candidate = Candidate(
        purpose="formal",
        dataset_id=dataset_id,
        manifest_sha256=manifest_sha,
        distribution=DISTRIBUTION,
        grouping_evidence="controlled generated examples",
        custodian=custodian,
        exposed_before_freeze=False,
    )
    register = formal.CampaignRegister(root / "governance")
    register.register(campaign_id, candidate)
    register.plan(campaign_id, fixture.plan)
    register.validation_native(campaign_id, fixture.request, lambda *_: snapshot)
    draft_data = _draft_data(candidate, fixture.plan, snapshot)
    if interleaved:
        draft_data.update(schema_version="ac-formal-freeze-draft-v3", schedule="REPETITION_CASE_CONDITION")
    draft = parse_freeze_draft(draft_data)
    confirmation = FreezeConfirmation(
        draft_sha256=formal._sha(draft), decision="CONFIRMED", human=custodian
    )
    frozen = {
        "schema_version": "ac-formal-freeze-v2",
        "campaign_id": campaign_id,
        "draft": draft.model_dump(mode="json"),
        "selected": formal._formal_selection(snapshot),
        "confirmation": confirmation.model_dump(mode="json"),
        "human_identity": formal.HUMAN_IDENTITY,
    }
    # Historical handoff fixtures start frozen; public-flow tests use actual freeze.
    if frozen_record:
        with register._campaign(campaign_id, writing=True) as (_, fd):
            write_once(fd, "freeze.json", frozen)
    request = Unlock(
        freeze_sha256=formal._hash(json_bytes(frozen)),
        directory=str(package_path),
        handoff=custodian,
    )
    return HandoffFixture(register, package_path, request, campaign_id, frozen)


@pytest.fixture
def prepared(tmp_path):
    return _prepared(tmp_path.resolve())


def _rejected(operation, code):
    with pytest.raises(BusinessError) as exc:
        operation()
    assert exc.value.code == code
    return str(exc.value)


def test_public_gate_and_preflight_never_read_candidate(prepared, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("public preflight/disabled commands attempted private content read")

    monkeypatch.setattr(formal, "load_private_package", forbidden)
    monkeypatch.setattr(formal, "reopen_private_package", forbidden)
    register = prepared.register
    preflight = register.preflight(prepared.campaign_id)
    assert preflight["status"] == "FROZEN" and preflight["execution_available"] is False
    assert preflight["freeze_sha256"] == prepared.request.freeze_sha256
    _rejected(
        lambda: register.unlock(prepared.campaign_id, prepared.request),
        "FORMAL_UNLOCK_BLOCKED",
    )
    _rejected(
        lambda: register.open_for_execution(
            prepared.campaign_id,
            expected_freeze_sha256=prepared.request.freeze_sha256,
            execution_cap=CAP,
            operator=OPERATOR,
        ),
        "FORMAL_EXECUTION_BLOCKED",
    )
    assert not prepared.journal.exists()


def test_unlock_and_reopen_record_actual_reads_and_bind_once(prepared, monkeypatch):
    original_load = formal.load_private_package
    original_reopen = formal.reopen_private_package

    def tracked_load(*args, **kwargs):
        assert [event["kind"] for event in prepared.events()] == ["OPENING"]
        return original_load(*args, **kwargs)

    def tracked_reopen(binding, **kwargs):
        last = prepared.events()[-1]
        assert last["kind"] == "REOPENING"
        assert last["human"]["actor"] == OPERATOR.actor
        return original_reopen(binding, **kwargs)

    monkeypatch.setattr(formal, "load_private_package", tracked_load)
    monkeypatch.setattr(formal, "reopen_private_package", tracked_reopen)
    response = prepared.unlock()
    assert response["status"] == "UNLOCKED" and response["execution_available"] is True
    assert str(prepared.package_path) not in repr(response)
    opened, read, terminal = prepared.events()
    assert [row["kind"] for row in (opened, read, terminal)] == [
        "OPENING",
        "CASES_READ",
        "UNLOCKED",
    ]
    assert opened["read_id"] == read["read_id"] == terminal["read_id"]
    assert terminal["freeze_sha256"] == prepared.request.freeze_sha256
    assert terminal["package_binding"]["directory"] == str(prepared.package_path)
    assert terminal["package_binding"]["inode"] == prepared.package_path.stat().st_ino
    assert "package_binding" not in repr(prepared.register.preflight(prepared.campaign_id))

    handoff = prepared.reopen()
    assert (
        handoff.package.binding.model_dump(mode="json", exclude_none=True)
        == terminal["package_binding"]
    )
    assert handoff.freeze_sha256 == terminal["freeze_sha256"]
    assert handoff.draft.model_dump(mode="json") == prepared.frozen["draft"]
    assert set(handoff.controls) == set(handoff.draft.conditions)
    assert str(prepared.package_path) not in repr(handoff)
    handoff.draft.block_order.clear()
    handoff.controls.clear()
    assert len(handoff.draft.block_order) == len(handoff.controls) == 4
    assert all(
        row["human_review"] == "PENDING" for row in handoff.package.load_references().values()
    )
    reopening, compared, reopened = prepared.events()[3:]
    assert [row["kind"] for row in (reopening, compared, reopened)] == [
        "REOPENING",
        "CASES_READ",
        "REOPENED",
    ]
    assert reopening["read_id"] == compared["read_id"] == reopened["read_id"] != terminal["read_id"]
    assert compared["matching_case_fingerprints_sha256"] == formal.digest(read["case_fingerprints"])
    assert "case_fingerprints" not in compared
    assert "package_binding" not in reopened
    assert prepared.reopen().package.binding == handoff.package.binding


@pytest.mark.parametrize(
    "failure", ["different", "malformed", "crash", "wrong-directory", "journal-write", "readback"]
)
def test_fresh_failure_cannot_reuse_old_success(prepared, monkeypatch, failure):
    prepared.unlock()
    if failure in {"different", "malformed"}:
        cases, _ = _synthetic()
        cases[0]["message"] = "new candidate phrase after the first successful opening"
        _write(
            prepared.package_path / "cases.json", b"[broken" if failure == "malformed" else cases
        )
    elif failure == "wrong-directory":
        prepared.package_path.rename(prepared.package_path.with_name("retired"))
        prepared.package_path.mkdir(mode=0o700)
    elif failure == "crash":

        def crash(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(formal, "reopen_private_package", crash)
    elif failure == "journal-write":
        append = formal.append_event

        def broken(fd, event):
            if event["kind"] in {"REOPENED", "EXPOSURE_UNCERTAIN"}:
                raise OSError("private internal failure")
            append(fd, event)

        monkeypatch.setattr(formal, "append_event", broken)
    else:

        def broken_readback(*args, **kwargs):
            raise OSError("private internal failure")

        monkeypatch.setattr(formal, "_verify_readback", broken_readback)

    if failure == "crash":
        with pytest.raises(KeyboardInterrupt):
            prepared.reopen()
    else:
        message = _rejected(prepared.reopen, "FORMAL_PACKAGE_OPEN_FAILED")
        assert str(prepared.package_path) not in message and "internal failure" not in message
    assert prepared.register.preflight(prepared.campaign_id)["status"] == "EXPOSURE_UNCERTAIN"
    _rejected(prepared.reopen, "FORMAL_EXECUTION_BLOCKED")
    events = prepared.events()
    assert events[3]["kind"] == "REOPENING"
    if failure == "different":
        assert events[4]["case_fingerprints"]
        assert events[4]["case_fingerprints"] != events[1]["case_fingerprints"]
    if failure == "malformed":
        assert events[4]["kind"] == "CASES_READ" and events[4]["case_fingerprints"] == []
    if failure == "readback":
        assert [event["kind"] for event in events[-2:]] == ["REOPENED", "EXPOSURE_UNCERTAIN"]
    assert sum(event["kind"] == "UNLOCKED" for event in events) == 1


def test_frozen_identity_snapshot_and_cap_checked_before_reopen(prepared, monkeypatch):
    prepared.unlock()

    def forbidden(*args, **kwargs):
        pytest.fail("invalid execution binding must block before opening candidate")

    monkeypatch.setattr(formal, "reopen_private_package", forbidden)
    _rejected(lambda: prepared.reopen(expected="1" * 64), "FORMAL_FREEZE_MISMATCH")
    cap = FreezeQuota(calls=11, budget_microusd=1200)
    _rejected(lambda: prepared.reopen(cap=cap), "FORMAL_BUDGET_CAP_EXCEEDED")
    snapshot = prepared.journal.with_name("validation-native.json")
    changed = json_data(snapshot.read_bytes())
    changed["rationale"] = "tampered validation rationale"
    snapshot.write_bytes(json_bytes(changed))
    _rejected(prepared.reopen, "FORMAL_FREEZE_MISMATCH")
    assert len(prepared.events()) == 3


@pytest.mark.parametrize("damage", ["different-read-id", "incomplete-line"])
def test_edited_terminal_cannot_be_reused(prepared, monkeypatch, damage):
    prepared.unlock()
    if damage == "different-read-id":
        events = prepared.events()
        events[-1]["read_id"] = "00000000-0000-0000-0000-000000000001"
        prepared.journal.write_bytes(b"".join(json_bytes(event) for event in events))
    else:
        prepared.journal.write_bytes(prepared.journal.read_bytes().removesuffix(b"\n"))

    def forbidden(*args, **kwargs):
        pytest.fail("inconsistent read identity must block before opening candidate")

    monkeypatch.setattr(formal, "reopen_private_package", forbidden)
    _rejected(prepared.reopen, "FORMAL_JOURNAL_INVALID")


@pytest.mark.parametrize("damage", ["permission", "hardlink"])
def test_journal_with_private_path_must_be_private_before_read_or_append(prepared, damage):
    prepared.unlock()
    path = prepared.journal
    original = path.read_bytes()
    if damage == "permission":
        path.chmod(0o644)
        read_code = write_code = "FORMAL_PACKAGE_PERMISSIONS"
    else:
        os.link(path, prepared.package_path.parent / "exposed-ledger")
        read_code, write_code = "FORMAL_FILE_INVALID", "FORMAL_STATE_INVALID"
    _rejected(lambda: prepared.register.preflight(prepared.campaign_id), read_code)
    with prepared.register._campaign(prepared.campaign_id, writing=True) as (_, fd):
        _rejected(lambda: append_event(fd, {"unexpected": "append"}), write_code)
    assert path.read_bytes() == original


@pytest.mark.parametrize("unknown_reopen", [False, True])
def test_second_campaign_cannot_hide_prior_exposure(prepared, unknown_reopen):
    prepared.unlock()
    prepared.reopen()
    if unknown_reopen:
        _write(prepared.package_path / "cases.json", b"[broken")
        _rejected(prepared.reopen, "FORMAL_PACKAGE_OPEN_FAILED")

    def rename(cases, refs):
        for row in cases:
            old = row["id"]
            row["id"] = f"ALIAS-{old}"
            refs[row["id"]] = {**refs.pop(old), "case_id": row["id"]}

    other = _prepared(prepared.package_path.parent, campaign_id="two", changes=rename)
    expected = "FORMAL_OTHER_EXPOSURE_UNCERTAIN" if unknown_reopen else "FORMAL_PACKAGE_OPEN_FAILED"
    _rejected(other.unlock, expected)
    if unknown_reopen:
        assert not other.journal.exists()
    else:
        assert [event["kind"] for event in other.events()] == [
            "OPENING",
            "CASES_READ",
            "EXPOSURE_UNCERTAIN",
        ]
