"""Read a committed formal candidate without exposing its files to the ordinary dataset loader."""

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from aftercare.business.errors import BusinessError
from aftercare.business.rules import INTENTS, MATERIAL_KINDS, digest, parse_time
from aftercare.business.service import FACT_VALUES
from aftercare.config import ROOT
from aftercare.intelligence.policy_versions import PolicyVersion

from ..dataset import material_bytes
from ..gates import GoldApproval
from ..retrieval_evidence import catalogue_sha256, fixture_policies
from .contracts import Digest, Identifier
from .files import MAX_PACKAGE, check_private, directory, fail, json_bytes, json_data, read_private

_CASE_ID = re.compile(r"[A-Z0-9-]{1,64}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_CHECK_PATH = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_INTENTS = {"damaged", "wrong_item", "refund_status"}
_ACTIONS = {"REFUND", "REPLACE"}
_ACTORS = {"customer", "outsider", "support", "approver"}
_ORDER_STATUSES = {"UNPAID", "CANCELLED", "PAID", "SHIPPED", "DELIVERED"}
_FILES = {"manifest.json", "cases.json", "references.json"}
_GOLD_FILE = "gold-approvals.json"
_MAX_CASES = 144
_MAX_GOLD = 256 * 1024
_EVENT_FIELDS = {
    "create_case": ({"order_id", "intent", "description"}, {"line_ids"}),
    "read": (set(), set()),
    "start": (set(), set()),
    "resume": (set(), set()),
    "upload": ({"kind", "asset_id"}, set()),
    "review": ({"decision", "notes"}, set()),
    "facts": ({"facts", "notes"}, {"line_ids"}),
    "propose": ({"action", "lines", "notes", "policy_ids"}, set()),
    "approve": ({"decision", "notes"}, set()),
    "execute": (set(), set()),
    "reconcile": (set(), set()),
    "repeat_event": ({"event_id"}, set()),
}
_REFERENCE_FIELDS = {
    "case_id",
    "family",
    "intent",
    "checks",
    "required_facts",
    "required_policy_ids",
    "allowed_draft_actions",
    "refund_amount_cents",
    "forbidden_actions",
    "label_source",
    "human_review",
}
_UNVERIFIED_GOLD_FIELDS = {"gold_approval", "original_label_source", "reviewer_id", "reviewed_at"}


class _PrivateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PackageManifestV2(_PrivateDocument):
    schema_version: Literal["ac-formal-package-v2"]
    dataset_id: Identifier
    cases_sha256: Digest
    references_sha256: Digest
    policy_catalogues_sha256: Digest
    gold_approvals_sha256: Digest | None = None


class PackageBinding(_PrivateDocument):
    """For the private UNLOCKED journal only."""

    directory: str = Field(repr=False, min_length=1, max_length=4096)
    device: int = Field(strict=True, ge=0)
    inode: int = Field(strict=True, gt=0)
    manifest_sha256: Digest
    cases_sha256: Digest
    references_sha256: Digest
    effective_reference_sha256: Digest
    policy_catalogues_sha256: Digest
    gold_approvals_sha256: Digest | None = None


class _FormalGoldApproval(GoldApproval):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    dataset_id: Identifier


@dataclass(frozen=True, slots=True)
class PrivatePackage:
    """An immutable verified view. Every consumer receives fresh JSON objects."""

    manifest: PackageManifestV2 = field(repr=False)
    binding: PackageBinding = field(repr=False)
    _cases: bytes = field(repr=False)
    _raw_references: bytes = field(repr=False)
    _effective_references: bytes = field(repr=False)
    _gold: bytes = field(repr=False)
    _assets: frozenset[tuple[str, str]] = field(repr=False)

    def load_cases(self, split: str | None = None) -> list[dict]:
        return json_data(self._cases) if split in {None, "holdout"} else []

    def load_references(self) -> dict[str, dict]:
        return json_data(self._effective_references)

    def load_raw_references(self) -> dict[str, dict]:
        return json_data(self._raw_references)

    def load_gold_approvals(self) -> dict[str, dict]:
        """Return bound, unauthenticated declarations for private inspection only."""
        return json_data(self._gold)

    def material_bytes(self, case_id: str, asset_id: str) -> bytes:
        if (case_id, asset_id) not in self._assets:
            fail("FORMAL_PACKAGE_ASSET_INVALID", "只能加载案例声明的合成材料。")
        if asset_id == "invalid-bytes":
            return b"not an image"  # Same synthetic negative fixture as ScriptReplay.
        return material_bytes(case_id, asset_id)


def _require(condition):
    if not condition:
        fail("FORMAL_PACKAGE_CONTENT_INVALID", "候选案例、脚本或参考的结构和关联不符合要求。")


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _string(value, *, minimum=1, maximum=2000):
    return isinstance(value, str) and minimum <= len(value.strip()) <= len(value) <= maximum


def _integer(value, *, minimum=0, maximum=2**31 - 1):
    return type(value) is int and minimum <= value <= maximum


def _timestamp(value):
    return _string(value, maximum=64) and parse_time(value) is not None


def _fields(value, required, optional=frozenset()):
    return isinstance(value, dict) and required <= value.keys() <= required | optional


def _keys(values, *, maximum=64):
    return (
        isinstance(values, list)
        and len(values) <= maximum
        and all(_matches(_KEY, value) for value in values)
        and len(set(values)) == len(values)
    )


def _by_id(rows, *, key="id", maximum=64, minimum=0):
    _require(isinstance(rows, list) and minimum <= len(rows) <= maximum)
    _require(all(isinstance(row, dict) and _matches(_KEY, row.get(key)) for row in rows))
    mapping = {row[key]: row for row in rows}
    _require(len(mapping) == len(rows))
    return mapping


def _amount_lines(rows, line_ids, amount_key):
    by_id = _by_id(rows, key="line_id", minimum=1)
    _require(by_id.keys() <= line_ids)
    _require(
        all(
            _fields(row, {"line_id", amount_key}) and _integer(row[amount_key], minimum=1)
            for row in rows
        )
    )


def _order_amounts(order, lines):
    amounts = ("discount_cents", "payable_cents", "paid_cents", "refunded_cents")
    subtotal = 0
    for line in lines:
        _require(all(_integer(line.get(key)) for key in (*amounts, "unit_price_cents")))
        gross = line["quantity"] * line["unit_price_cents"]
        _require(gross - line["discount_cents"] == line["payable_cents"])
        _require(line["refunded_cents"] <= line["paid_cents"] <= line["payable_cents"])
        subtotal += gross
    _require(_integer(order.get("subtotal_cents")) and order["subtotal_cents"] == subtotal)
    for key in amounts:
        _require(_integer(order.get(key)) and order[key] == sum(line[key] for line in lines))


def _business_fixture(fixture):
    _require(
        _fields(fixture, {"as_of", "business"}, {"inventory", "refund_history", "policy_overrides"})
    )
    _require(_timestamp(fixture["as_of"]))
    business = fixture["business"]
    _require(_fields(business, {"customers", "products", "orders", "shipments", "policies"}))
    customers = _by_id(business["customers"], minimum=1)
    products = _by_id(business["products"], minimum=1)
    orders = _by_id(business["orders"], minimum=1)
    _require("not-owner" not in customers)
    owner = next(iter(customers))
    first_order = next(iter(orders.values()))
    _require(first_order.get("customer_id") == owner)

    lines_by_order = {}
    for order_id, order in orders.items():
        _require(order.get("customer_id") in customers)
        _require(order.get("status") in _ORDER_STATUSES and _timestamp(order.get("created_at")))
        for key in ("paid_at", "shipped_at", "delivered_at", "cancelled_at"):
            _require(order.get(key) is None or _timestamp(order[key]))
        lines = _by_id(order.get("items"), key="line_id", minimum=1)
        lines_by_order[order_id] = lines.keys()
        for line in lines.values():
            _require(
                line.get("product_id") in products and _integer(line.get("quantity"), minimum=1)
            )
        _order_amounts(order, lines.values())

    shipments = _by_id(business["shipments"], key="order_id")
    _require(shipments.keys() <= orders.keys())
    for shipment in shipments.values():
        _require(_matches(_KEY, shipment.get("id")) and _matches(_KEY, shipment.get("status")))
        for key in ("shipped_at", "delivered_at"):
            _require(shipment.get(key) is None or _timestamp(shipment[key]))

    inventory = _by_id(fixture.get("inventory", []), key="product_id")
    _require(inventory.keys() <= products.keys())
    _require(all(_integer(row.get("available_quantity")) for row in inventory.values()))
    _validate_history(fixture.get("refund_history", []), lines_by_order[first_order["id"]])

    for group in (business["policies"], fixture.get("policy_overrides", [])):
        _require(isinstance(group, list) and len(group) <= 128)
    policies = fixture_policies(fixture)
    policy_keys = set()
    for value in policies:
        policy = PolicyVersion.model_validate(value)
        _require(_matches(_KEY, policy.id) and _matches(_KEY, policy.version))
        _require(_timestamp(value.get("effective_at")))
        _require(_string(value.get("title")) and isinstance(value.get("parameters", {}), dict))
        key = (policy.id, policy.version)
        _require(key not in policy_keys)
        policy_keys.add(key)
    return lines_by_order, {key[0] for key in policy_keys}


def _validate_history(rows, line_ids):
    history = _by_id(rows)
    seen_scopes = set()
    for row in history.values():
        _require(len(row["id"]) <= 36 and _timestamp(row.get("created_at")))
        intent = row.get("intent", "return")
        _require(intent in INTENTS)
        _require(row.get("status") in {"SUBMITTING", "UNKNOWN", "FAILED", "SUCCEEDED"})
        _require(_integer(row.get("amount_cents"), minimum=1))
        _amount_lines(row.get("lines"), line_ids, "amount_cents")
        _require(sum(line["amount_cents"] for line in row["lines"]) == row["amount_cents"])
        refund_lines = [line["line_id"] for line in row["lines"]]
        scope = row.get("case_line_ids", refund_lines)
        _require(_keys(scope) and set(refund_lines) <= set(scope) <= line_ids)
        scope_key = (intent, tuple(sorted(scope)))
        _require(scope_key not in seen_scopes)
        seen_scopes.add(scope_key)
        receipt = row.get("receipt")
        if row["status"] == "SUCCEEDED":
            _require(_fields(receipt, {"id", "mode", "result", "amount_cents"}))
            _require(
                _matches(_KEY, receipt["id"])
                and receipt["mode"] == "SIMULATED"
                and receipt["result"] == "REFUND_RECORDED"
                and type(receipt["amount_cents"]) is int
                and receipt["amount_cents"] == row["amount_cents"]
            )
        else:
            _require("receipt" in row and receipt is None)


def _event_args(op, args, *, line_ids, policy_ids):
    if "notes" in args:
        _require(_string(args["notes"]))
    if "line_ids" in args and args["line_ids"] is not None:
        keys = args["line_ids"]
        _require(_keys(keys) and bool(keys) and set(keys) <= line_ids)
    if op == "upload":
        _require(args["kind"] in MATERIAL_KINDS and _matches(_ASSET_ID, args["asset_id"]))
    if op in {"review", "approve"}:
        decisions = {"APPROVED", "REJECTED"}
        if op == "review":
            decisions.add("NEEDS_MORE")
        _require(args["decision"] in decisions)
    if op == "facts":
        facts = args["facts"]
        _require(isinstance(facts, dict) and bool(facts))
        _require(
            all(key in FACT_VALUES and value in FACT_VALUES[key] for key, value in facts.items())
        )
    if op == "propose":
        action = args["action"]
        _require(action in _ACTIONS and _keys(args["policy_ids"]))
        _require(set(args["policy_ids"]) <= policy_ids)
        _amount_lines(args["lines"], line_ids, "amount_cents" if action == "REFUND" else "quantity")


def _case(case):
    _require(_fields(case, {"id", "split", "message", "fixture", "events"}))
    _require(_matches(_CASE_ID, case["id"]) and not case["id"].startswith("AC02-"))
    _require(case["split"] == "holdout" and _string(case["message"], minimum=5))
    order_lines, policy_ids = _business_fixture(case["fixture"])
    events = case["events"]
    _require(isinstance(events, list) and 1 <= len(events) <= 64)
    intent, line_ids, assets = None, set(), set()
    seen = {}
    for event in events:
        _require(_fields(event, {"id", "op", "actor", "args"}))
        key, op, actor, args = (event["id"], event["op"], event["actor"], event["args"])
        _require(_matches(_KEY, key) and key not in seen)
        _require(op in _EVENT_FIELDS and actor in _ACTORS)
        _require(_fields(args, *_EVENT_FIELDS[op]))
        if op == "create_case":
            _require(intent is None and not seen)
            intent = args["intent"]
            _require(intent in _INTENTS and args["order_id"] in order_lines)
            _require(_string(args["description"], minimum=5))
            line_ids = order_lines[args["order_id"]]
        else:
            _require(intent is not None)
        if op == "start":
            _require("start" not in seen.values())
        if op == "resume":
            _require("start" in seen.values())
        if op == "repeat_event":
            _require(seen.get(args["event_id"]) == "execute")
        _event_args(op, args, line_ids=line_ids, policy_ids=policy_ids)
        if op == "upload":
            assets.add((case["id"], args["asset_id"]))
        seen[key] = op
    return intent, set(seen), policy_ids, assets


def _validate_reference(case_id, reference, intent, event_ids, policy_ids):
    _require(_fields(reference, _REFERENCE_FIELDS, _UNVERIFIED_GOLD_FIELDS))
    _require(reference["case_id"] == case_id and reference["intent"] == intent)
    _require(_matches(_KEY, reference["family"]))
    for field_name in (
        "required_facts",
        "required_policy_ids",
        "allowed_draft_actions",
        "forbidden_actions",
    ):
        _require(_keys(reference[field_name]))
    _require(set(reference["allowed_draft_actions"]) <= _ACTIONS)
    _require(set(reference["required_policy_ids"]) <= policy_ids)
    _require(_string(reference["label_source"], maximum=64))
    _require(_string(reference["human_review"], maximum=64))
    checks = reference["checks"]
    _require(isinstance(checks, list) and 1 <= len(checks) <= 100)
    checked = set()
    for check in checks:
        _require(_fields(check, {"event_id", "path", "equals"}))
        _require(check["event_id"] in event_ids and _matches(_CHECK_PATH, check["path"]))
        _require(len(check["path"]) <= 256)
        _require(check["path"] != "detail.code" or _matches(_KEY, check["equals"]))
        key = (check["event_id"], check["path"])
        _require(key not in checked)
        checked.add(key)
    limits = reference["refund_amount_cents"]
    if limits is not None:
        _require(_fields(limits, {"min", "max"}))
        _require(_integer(limits["min"]) and _integer(limits["max"], minimum=limits["min"]))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _verify(raw, expected):
    if _sha(raw) != expected:
        fail("FORMAL_PACKAGE_DIGEST_MISMATCH", "候选包文件摘要与承诺不一致。")


def _read_cases(fd, expected, on_cases):
    raw = read_private(fd, "cases.json", maximum=MAX_PACKAGE, on_read_failure=lambda: on_cases([]))
    try:
        cases = _decode(raw)
        exposed = deepcopy(cases) if isinstance(cases, list) else []
    except (ValueError, RecursionError, OverflowError):
        on_cases([])
        raise BusinessError(
            "FORMAL_PACKAGE_CONTENT_INVALID", "候选案例文件格式不符合要求。"
        ) from None
    on_cases(exposed)
    _verify(raw, expected)
    return cases


def _decode(raw):
    value = json_data(raw)
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting is too deep")
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)
        elif isinstance(node, float) and not math.isfinite(node):
            raise ValueError("Non-finite JSON number")
    return value


def _verified_references(manifest, raw, gold_raw, cases, validation):
    references = _decode(raw)
    case_ids = set(cases)
    _require(isinstance(references, dict) and references.keys() == case_ids)
    gold = {}
    if gold_raw is not None:
        rows = TypeAdapter(list[_FormalGoldApproval]).validate_python(_decode(gold_raw))
        _require(len(rows) <= _MAX_CASES)
        for approval in rows:
            case_id = approval.case_id
            if (
                approval.dataset_id != manifest.dataset_id
                or case_id not in case_ids
                or case_id in gold
                or approval.case_sha256 != digest(cases[case_id])
                or approval.reference_sha256 != digest(references[case_id])
            ):
                fail("FORMAL_PACKAGE_GOLD_MISMATCH", "金标声明未唯一匹配该案例和原始参考。")
            gold[case_id] = approval

    effective = {}
    for case_id, reference in references.items():
        intent, events, policies, _ = validation[case_id]
        _validate_reference(case_id, reference, intent, events, policies)
        selected = {
            key: value for key, value in reference.items() if key not in _UNVERIFIED_GOLD_FIELDS
        }
        selected.update(label_source="SYNTHETIC_REFERENCE", human_review="PENDING")
        effective[case_id] = selected
    declarations = {
        case_id: {
            **row.model_dump(mode="json"),
            "identity_verification": "UNAUTHENTICATED_DECLARATION",
        }
        for case_id, row in gold.items()
    }
    return effective, declarations


def load_private_package(
    path: str | Path,
    *,
    manifest_sha256: str,
    dataset_id: str,
    distribution: Mapping[str, int],
    on_cases: Callable[[list], None],
    expected_binding: PackageBinding | None = None,
) -> PrivatePackage:
    """Open a v2 candidate after the caller has durably recorded OPENING.

    on_cases is called with a separate parsed copy before digest or structure validation.
    An empty list signals unreadable or malformed contents after the read began.
    """
    try:
        return _load(path, manifest_sha256, dataset_id, distribution, on_cases, expected_binding)
    except OSError:
        raise BusinessError(
            "FORMAL_PACKAGE_UNAVAILABLE", "候选包的固定文件不可安全读取。"
        ) from None
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise BusinessError(
            "FORMAL_PACKAGE_CONTENT_INVALID", "候选包文件格式不符合要求。"
        ) from None


def _load(path, manifest_sha256, dataset_id, distribution, on_cases, expected_binding):
    path = Path(path)
    _require(path.anchor == "/" and len(str(path)) <= 4096)
    root = os.stat(ROOT)
    with directory(path, excluded_identity=(root.st_dev, root.st_ino)) as fd:
        opened = os.fstat(fd)
        check_private(opened, directory_entry=True)
        if expected_binding is not None and (
            str(path),
            opened.st_dev,
            opened.st_ino,
            manifest_sha256,
        ) != (
            expected_binding.directory,
            expected_binding.device,
            expected_binding.inode,
            expected_binding.manifest_sha256,
        ):
            fail("FORMAL_PACKAGE_CHANGED", "候选包目录身份或清单承诺已变化。")

        raw_manifest = read_private(fd, "manifest.json")
        _verify(raw_manifest, manifest_sha256)
        manifest = PackageManifestV2.model_validate(_decode(raw_manifest))
        _require(manifest.dataset_id == dataset_id and dataset_id not in {"v0.1", "v0.2"})
        cases = _read_cases(fd, manifest.cases_sha256, on_cases)
        _require(isinstance(cases, list) and 1 <= len(cases) <= _MAX_CASES)
        validation = {}
        by_id = {}
        for row in cases:
            checked = _case(row)
            _require(row["id"] not in by_id)
            by_id[row["id"]] = row
            validation[row["id"]] = checked
        _require(
            set(distribution) == _INTENTS
            and all(
                _integer(value, minimum=1, maximum=_MAX_CASES) for value in distribution.values()
            )
            and Counter(value[0] for value in validation.values()) == dict(distribution)
        )
        policies = digest(
            {
                case_id: catalogue_sha256(fixture_policies(row["fixture"]))
                for case_id, row in by_id.items()
            }
        )
        if policies != manifest.policy_catalogues_sha256:
            fail("FORMAL_PACKAGE_POLICY_MISMATCH", "逐案政策目录摘要与承诺不一致。")

        names = _FILES | ({_GOLD_FILE} if manifest.gold_approvals_sha256 else set())
        if set(os.listdir(fd)) != names:
            fail("FORMAL_PACKAGE_LAYOUT_INVALID", "候选包须仅包含约定的固定文件。")
        raw_references = read_private(fd, "references.json", maximum=MAX_PACKAGE)
        _verify(raw_references, manifest.references_sha256)
        gold_raw = None
        if manifest.gold_approvals_sha256:
            gold_raw = read_private(fd, _GOLD_FILE, maximum=_MAX_GOLD)
            _verify(gold_raw, manifest.gold_approvals_sha256)
        effective, gold = _verified_references(
            manifest, raw_references, gold_raw, by_id, validation
        )
        final = os.fstat(fd)
        check_private(final, directory_entry=True)
        if (opened.st_mtime_ns, opened.st_ctime_ns) != (final.st_mtime_ns, final.st_ctime_ns):
            fail("FORMAL_PACKAGE_CHANGED", "读取期间候选包目录已发生变化。")

        binding = PackageBinding(
            directory=str(path),
            device=opened.st_dev,
            inode=opened.st_ino,
            manifest_sha256=manifest_sha256,
            cases_sha256=manifest.cases_sha256,
            references_sha256=manifest.references_sha256,
            effective_reference_sha256=digest(
                {case_id: digest(row) for case_id, row in effective.items()}
            ),
            policy_catalogues_sha256=policies,
            gold_approvals_sha256=manifest.gold_approvals_sha256,
        )
        if expected_binding is not None and binding != expected_binding:
            fail("FORMAL_PACKAGE_CHANGED", "候选包与成功解锁时的绑定已不一致。")
        assets = frozenset(asset for value in validation.values() for asset in value[3])
        return PrivatePackage(
            manifest,
            binding,
            json_bytes(cases),
            raw_references,
            json_bytes(effective),
            json_bytes(gold),
            assets,
        )


def reopen_private_package(
    binding: PackageBinding,
    *,
    dataset_id: str,
    distribution: Mapping[str, int],
    on_cases: Callable[[list], None],
) -> PrivatePackage:
    """Reopen only the private journal binding, after the caller records reopening.

    The caller must persist this read's case exposure too. New, malformed or
    partially read bytes can be exposed even when no verified view is returned.
    """
    return load_private_package(
        binding.directory,
        manifest_sha256=binding.manifest_sha256,
        dataset_id=dataset_id,
        distribution=distribution,
        on_cases=on_cases,
        expected_binding=binding,
    )
