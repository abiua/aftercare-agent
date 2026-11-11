import hashlib
import os
from dataclasses import FrozenInstanceError

import pytest

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.evaluation import dataset
from aftercare.evaluation.formal import package
from aftercare.evaluation.formal.files import json_bytes
from aftercare.evaluation.metrics import _gold
from aftercare.evaluation.retrieval_evidence import catalogue_sha256, fixture_policies

DATASET = "private-synthetic"
DISTRIBUTION = dict.fromkeys(("damaged", "wrong_item", "refund_status"), 1)
STAMP = "2026-11-09T12:00:00+08:00"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _write(path, raw):
    path.write_bytes(raw if isinstance(raw, bytes) else json_bytes(raw))
    path.chmod(0o600)
    return _sha(path.read_bytes())


def _case(index, intent):
    case_id = f"PRIVATE-{index}"
    baseline = {
        "products": [{"name": "模拟商品", "category": "TEST", "unit_price_cents": 1299}],
        "policies": [],
    }
    fixture = dataset._fixture(case_id, index, baseline)
    script = dataset.Script()
    message = "模拟订单售后情况"
    dataset._create(script, fixture, intent=intent, message=message, line_ids=["L01"])
    if intent == "damaged":
        script.photo("ITEM", "sample")
    event = script.add("start", "support")
    script.expect(event, "status", "WAITING_INPUT")
    return dataset._package(
        case_id,
        "holdout",
        "synthetic_family",
        intent,
        message,
        fixture,
        script,
        policy_ids=["POL13"] if intent == "wrong_item" else (),
    )


def _synthetic():
    pairs = [_case(i, intent) for i, intent in enumerate(DISTRIBUTION, 1)]
    return [case for case, _ in pairs], {case["id"]: ref for case, ref in pairs}


def _put(path, cases, references, *, gold=None, manifest_overrides=None):
    manifest = {
        "schema_version": "ac-formal-package-v2",
        "dataset_id": DATASET,
        "cases_sha256": _write(path / "cases.json", cases),
        "references_sha256": _write(path / "references.json", references),
        "policy_catalogues_sha256": digest(
            {row["id"]: catalogue_sha256(fixture_policies(row["fixture"])) for row in cases}
        )
        if isinstance(cases, list)
        else "0" * 64,
    }
    if gold is not None:
        manifest["gold_approvals_sha256"] = _write(path / "gold-approvals.json", gold)
    manifest.update(manifest_overrides or {})
    return _write(path / "manifest.json", manifest)


@pytest.fixture
def candidate(tmp_path):
    path = tmp_path.resolve() / "private-candidate"
    path.mkdir(mode=0o700)
    cases, references = _synthetic()
    return path, cases, references


def _open(path, sha, listener=None):
    return package.load_private_package(
        path,
        manifest_sha256=sha,
        dataset_id=DATASET,
        distribution=DISTRIBUTION,
        on_cases=listener if listener is not None else lambda _: None,
    )


def _reject(path, sha, code, listener=None):
    with pytest.raises(BusinessError) as exc:
        _open(path, sha, listener)
    assert exc.value.code == code
    assert str(path) not in str(exc.value)
    assert "PRIVATE-1" not in str(exc.value)


def test_immutable_view_uses_only_declared_synthetic_assets(candidate):
    path, cases, refs = candidate
    raw = refs["PRIVATE-1"]
    raw.update(label_source="INDEPENDENT_HUMAN", human_review="VERIFIED", reviewer_id="self")
    sha = _put(path, cases, refs)

    def observed(exposed):
        assert exposed == cases
        exposed[0]["fixture"]["business"]["orders"][0]["paid_cents"] = 0
        exposed.clear()

    view = _open(path, sha, observed)
    assert view.load_cases() == cases == view.load_cases("holdout")
    assert view.load_cases("validation") == []
    assert view.load_raw_references() == refs
    effective = view.load_references()
    assert effective["PRIVATE-1"]["label_source"] == "SYNTHETIC_REFERENCE"
    assert effective["PRIVATE-1"]["human_review"] == "PENDING"
    assert "reviewer_id" not in effective["PRIVATE-1"]
    assert view.load_gold_approvals() == {}
    effective["PRIVATE-1"]["checks"].clear()
    assert view.load_references()["PRIVATE-1"]["checks"]
    view.load_cases()[0]["fixture"].clear()
    assert view.load_cases()[0]["fixture"]
    assert view.binding.effective_reference_sha256 == digest(
        {case_id: digest(row) for case_id, row in view.load_references().items()}
    )
    assert view.material_bytes("PRIVATE-1", "item-sample").startswith(b"\x89PNG")
    for case_id, asset_id in (("PRIVATE-2", "item-sample"), ("PRIVATE-1", "../anything")):
        with pytest.raises(BusinessError) as exc:
            view.material_bytes(case_id, asset_id)
        assert exc.value.code == "FORMAL_PACKAGE_ASSET_INVALID"
    with pytest.raises(FrozenInstanceError):
        view.binding = None
    assert str(path) not in repr(view) and str(path) not in repr(view.binding)
    reopened = package.reopen_private_package(
        view.binding,
        dataset_id=DATASET,
        distribution=DISTRIBUTION,
        on_cases=observed,
    )
    assert reopened.binding == view.binding
    assert reopened.load_references() == view.load_references()


@pytest.mark.parametrize(
    "mismatch", [None, "case_sha256", "reference_sha256", "dataset_id", "duplicate"]
)
def test_gold_file_binds_original_case_and_reference(candidate, mismatch):
    path, cases, refs = candidate
    reference = refs["PRIVATE-1"]
    reference.update(
        label_source="INDEPENDENT_HUMAN",
        human_review="VERIFIED",
        reviewer_id="self-declared reviewer",
        reviewed_at=STAMP,
    )
    declaration = {
        "dataset_id": DATASET,
        "case_id": "PRIVATE-1",
        "case_sha256": digest(cases[0]),
        "reference_sha256": digest(reference),
        "reviewer_id": "declared reviewer",
        "reviewed_at": STAMP,
        "source": "synthetic test attestation",
    }
    if mismatch not in {None, "duplicate"}:
        declaration[mismatch] = "a" * 64 if mismatch.endswith("sha256") else "another-dataset"
    gold = [declaration, declaration] if mismatch == "duplicate" else [declaration]
    sha = _put(path, cases, refs, gold=gold)
    if mismatch:
        _reject(path, sha, "FORMAL_PACKAGE_GOLD_MISMATCH")
        return
    view = _open(path, sha)
    original = view.load_raw_references()["PRIVATE-1"]
    assert original["reviewer_id"] == "self-declared reviewer"
    assert _gold({"expected": original})
    effective = view.load_references()
    assert effective["PRIVATE-1"]["label_source"] == "SYNTHETIC_REFERENCE"
    assert effective["PRIVATE-1"]["human_review"] == "PENDING"
    assert "reviewer_id" not in effective["PRIVATE-1"]
    assert "gold_approval" not in effective["PRIVATE-1"]
    assert not _gold({"expected": effective["PRIVATE-1"]})
    assert effective["PRIVATE-2"]["human_review"] == "PENDING"
    assert view.load_gold_approvals() == {
        "PRIVATE-1": {**declaration, "identity_verification": "UNAUTHENTICATED_DECLARATION"}
    }
    assert view.binding.gold_approvals_sha256 == _sha((path / "gold-approvals.json").read_bytes())


@pytest.mark.parametrize("drift", ["manifest", "cases", "malformed-cases", "references", "policy"])
def test_committed_bytes_and_case_exposure_order(candidate, monkeypatch, drift):
    path, cases, refs = candidate
    manifest_overrides = {"policy_catalogues_sha256": "0" * 64} if drift == "policy" else {}
    sha = _put(path, cases, refs, manifest_overrides=manifest_overrides)
    files = []
    read_private = package.read_private

    def tracked(fd, name, **kwargs):
        files.append(name)
        return read_private(fd, name, **kwargs)

    def exposed(payload):
        assert payload == ([] if drift == "malformed-cases" else cases)
        files.append("CASES_READ")

    monkeypatch.setattr(package, "read_private", tracked)
    if drift == "malformed-cases":
        _write(path / "cases.json", b'{"bad":')
    elif drift != "policy":
        target = path / f"{drift}.json"
        _write(target, target.read_bytes() + b" ")
    code = {
        "malformed-cases": "FORMAL_PACKAGE_CONTENT_INVALID",
        "policy": "FORMAL_PACKAGE_POLICY_MISMATCH",
    }.get(drift, "FORMAL_PACKAGE_DIGEST_MISMATCH")
    _reject(path, sha, code, exposed)
    expected = ["manifest.json"]
    if drift != "manifest":
        expected.extend(["cases.json", "CASES_READ"])
    if drift == "references":
        expected.append("references.json")
    assert files == expected


@pytest.mark.parametrize(
    "invalid",
    [
        "dir-mode",
        "file-mode",
        "ownership",
        "file-link",
        "dir-link",
        "ancestor-link",
        "hardlink",
        "fifo",
        "extra-file",
        "oversized",
        "workspace",
    ],
)
def test_private_layout_and_os_safety(candidate, monkeypatch, invalid):
    path, cases, refs = candidate
    sha = _put(path, cases, refs)
    cases_path = path / "cases.json"
    requested = path
    code = "FORMAL_PACKAGE_UNAVAILABLE"
    if invalid == "dir-mode":
        path.chmod(0o750)
        code = "FORMAL_PACKAGE_PERMISSIONS"
    elif invalid == "file-mode":
        cases_path.chmod(0o640)
        code = "FORMAL_PACKAGE_PERMISSIONS"
    elif invalid == "ownership":
        monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
        code = "FORMAL_PACKAGE_PERMISSIONS"
    elif invalid == "file-link":
        target = path.parent / "other-cases.json"
        cases_path.rename(target)
        cases_path.symlink_to(target)
    elif invalid == "dir-link":
        requested = path.parent / "linked-candidate"
        requested.symlink_to(path, target_is_directory=True)
    elif invalid == "ancestor-link":
        alias = path.parent / "linked-parent"
        alias.symlink_to(path.parent, target_is_directory=True)
        requested = alias / path.name
    elif invalid == "hardlink":
        os.link(cases_path, path.parent / "hard-cases.json")
        code = "FORMAL_FILE_INVALID"
    elif invalid == "fifo":
        cases_path.unlink()
        os.mkfifo(cases_path, 0o600)
        code = "FORMAL_FILE_INVALID"
    elif invalid == "extra-file":
        _write(path / "extra.json", {})
        code = "FORMAL_PACKAGE_LAYOUT_INVALID"
    elif invalid == "oversized":
        with cases_path.open("r+b") as stream:
            stream.truncate(package.MAX_PACKAGE + 1)
        code = "FORMAL_FILE_INVALID"
    elif invalid == "workspace":
        monkeypatch.setattr(package, "ROOT", path.parent)
        code = "FORMAL_PACKAGE_PATH_FORBIDDEN"
    _reject(requested, sha, code)


def test_reopen_rejects_identical_directory_replacement(candidate, monkeypatch):
    path, cases, refs = candidate
    sha = _put(path, cases, refs)
    view = _open(path, sha)
    path.rename(path.parent / "original")
    path.mkdir(mode=0o700)
    assert _put(path, cases, refs) == sha

    def must_not_read(*args, **kwargs):
        pytest.fail("identity must be checked before reading candidate files")

    monkeypatch.setattr(package, "read_private", must_not_read)
    with pytest.raises(BusinessError) as exc:
        package.reopen_private_package(
            view.binding,
            dataset_id=DATASET,
            distribution=DISTRIBUTION,
            on_cases=must_not_read,
        )
    assert exc.value.code == "FORMAL_PACKAGE_CHANGED"


@pytest.mark.parametrize("malformed", [False, True])
def test_reopen_records_replaced_case_bytes_before_rejecting(candidate, malformed):
    path, cases, refs = candidate
    sha = _put(path, cases, refs)
    view = _open(path, sha)
    replacement = [{**cases[0], "id": "REPLACEMENT"}]
    (path / "cases.json").unlink()
    _write(path / "cases.json", b"[bad" if malformed else replacement)
    recorded = []
    with pytest.raises(BusinessError) as exc:
        package.reopen_private_package(
            view.binding,
            dataset_id=DATASET,
            distribution=DISTRIBUTION,
            on_cases=lambda exposed: recorded.append([digest(row) for row in exposed]),
        )
    assert exc.value.code == (
        "FORMAL_PACKAGE_CONTENT_INVALID" if malformed else "FORMAL_PACKAGE_DIGEST_MISMATCH"
    )
    assert recorded == ([[]] if malformed else [[digest(replacement[0])]])


@pytest.mark.parametrize("reopening", [False, True])
@pytest.mark.parametrize(
    ("interruption", "code"),
    [
        ("initial-stat", "FORMAL_PACKAGE_UNAVAILABLE"),
        ("final-stat", "FORMAL_PACKAGE_UNAVAILABLE"),
        ("permissions", "FORMAL_PACKAGE_PERMISSIONS"),
        ("link-count", "FORMAL_PACKAGE_CHANGED"),
    ],
)
def test_case_read_interruption_records_exposure(
    candidate, monkeypatch, reopening, interruption, code
):
    path, cases, refs = candidate
    sha = _put(path, cases, refs)
    binding = _open(path, sha).binding if reopening else None
    case_file = path / "cases.json"
    inode = case_file.stat().st_ino
    real_fstat = os.fstat
    reads = []
    exposure = []

    def interrupted(fd):
        info = real_fstat(fd)
        if info.st_ino != inode:
            return info
        reads.append(info)
        if interruption == "initial-stat" or (interruption == "final-stat" and len(reads) == 2):
            raise OSError("private path must never enter public error")
        if len(reads) == 2:
            if interruption == "permissions":
                case_file.chmod(0o640)
            else:
                os.link(case_file, path.parent / "new-link")
            return real_fstat(fd)
        return info

    monkeypatch.setattr(os, "fstat", interrupted)
    if reopening:
        with pytest.raises(BusinessError) as exc:
            package.reopen_private_package(
                binding,
                dataset_id=DATASET,
                distribution=DISTRIBUTION,
                on_cases=exposure.append,
            )
        assert exc.value.code == code
        assert "private path" not in str(exc.value)
    else:
        _reject(path, sha, code, exposure.append)
    assert exposure == ([] if interruption == "initial-stat" else [[]])


@pytest.mark.parametrize(
    "invalid",
    [
        "fault",
        "file-path",
        "order-line",
        "event-repeat",
        "duplicate-event",
        "reference-event",
        "policy-id",
        "foreign-product",
        "simulated-receipt",
        "duplicate-history-scope",
        "inconsistent-cents",
        "non-scalar-error-code",
        "wrong-split",
        "duplicate-case",
    ],
)
def test_scripts_fixtures_and_references_are_validated(candidate, invalid):
    path, cases, refs = candidate
    damaged = cases[0]
    events = damaged["events"]
    if invalid == "fault":
        events[1].update(op="fault", args={"action": "break"})
    elif invalid == "file-path":
        events[1]["args"]["asset_id"] = "../source.png"
    elif invalid == "order-line":
        events[0]["args"]["line_ids"] = ["UNKNOWN"]
    elif invalid == "event-repeat":
        events[1].update(op="repeat_event", args={"event_id": events[0]["id"]})
    elif invalid == "duplicate-event":
        events[1]["id"] = events[0]["id"]
    elif invalid == "reference-event":
        refs[damaged["id"]]["checks"][0]["event_id"] = "unknown"
    elif invalid == "policy-id":
        refs[damaged["id"]]["required_policy_ids"] = ["MISSING"]
    elif invalid == "foreign-product":
        damaged["fixture"]["business"]["orders"][0]["items"][0]["product_id"] = "MISSING"
    elif invalid in {"simulated-receipt", "duplicate-history-scope"}:
        line = dataset._order(damaged["fixture"])["items"][0]
        dataset._history(damaged["fixture"], line, "SUCCEEDED", 1)
        if invalid == "simulated-receipt":
            damaged["fixture"]["refund_history"][0]["receipt"]["mode"] = "LIVE"
        else:
            dataset._history(damaged["fixture"], line, "UNKNOWN", 2)
    elif invalid == "inconsistent-cents":
        damaged["fixture"]["business"]["orders"][0]["paid_cents"] += 1
    elif invalid == "non-scalar-error-code":
        refs[damaged["id"]]["checks"][0].update(path="detail.code", equals=[])
    elif invalid == "wrong-split":
        damaged["split"] = "validation"
    else:
        cases[1]["id"] = damaged["id"]
    sha = _put(path, cases, refs)
    _reject(path, sha, "FORMAL_PACKAGE_CONTENT_INVALID")


def test_exposure_failure_prevents_any_reference_read(candidate, monkeypatch):
    path, cases, refs = candidate
    sha = _put(path, cases, refs)
    original = package.read_private

    def tracked(fd, name, **kwargs):
        assert name in {"manifest.json", "cases.json"}
        return original(fd, name, **kwargs)

    def journal_unavailable(_):
        raise BusinessError("JOURNAL_FAILED", "暴露登记写入失败。")

    monkeypatch.setattr(package, "read_private", tracked)
    _reject(path, sha, "JOURNAL_FAILED", journal_unavailable)
