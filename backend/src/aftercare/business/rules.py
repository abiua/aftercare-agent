from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

INTENTS = frozenset({"damaged", "wrong_item", "refund_status", "cancel", "return", "warranty"})
MATERIAL_KINDS = frozenset({"ITEM", "PARCEL", "WRONG_ITEM", "LABEL"})
PENDING_EXECUTION = frozenset({"SUBMITTING", "UNKNOWN"})
POLICIES_BY_INTENT = {
    "damaged": ["POL01", "POL02", "POL06", "POL09", "POL10", "POL14"],
    "wrong_item": ["POL01", "POL13"],
    "refund_status": ["POL01", "POL08", "POL12"],
    "cancel": ["POL01", "POL02", "POL09", "POL10"],
    "return": ["POL01", "POL02", "POL05", "POL07", "POL09", "POL10"],
    "warranty": ["POL01", "POL11"],
}
ACTION_POLICY_IDS = {
    "REFUND": {"POL02", "POL09", "POL10"},
    "REPLACE": {"POL13"},
}


def now() -> datetime:
    return datetime.now(UTC)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def digest(data: object) -> str:
    content = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def scoped_policy_documents(resolution: dict, intent: str, order_status: str) -> list[dict]:
    """Build a common authority for the selected lines and flag mixed terms."""
    products = list(resolution["by_product"].values())
    if not products:
        return []
    per_product = [{policy["id"]: policy for policy in documents} for documents in products]
    common = set.intersection(*(set(index) for index in per_product))
    relevant = set(POLICIES_BY_INTENT[intent])
    if intent == "cancel":
        relevant.add("POL04" if order_status == "SHIPPED" else "POL03")
    if intent == "wrong_item":
        relevant |= ACTION_POLICY_IDS["REFUND"]
    documents = []
    for key in sorted(common):
        candidates = [index[key] for index in per_product]
        if len({digest(policy.get("parameters", {})) for policy in candidates}) > 1:
            if key in relevant:
                resolution["blockers"].append(f"INCOMPATIBLE_SCOPED_TERMS:{key}")
            continue
        documents.append(candidates[0])
    return documents


def eligibility(
    *,
    case: dict,
    order: dict,
    policies: list[dict],
    materials: list[dict],
    review: dict | None,
    facts: dict,
    balances: list[dict],
    policy_blockers: list[str] | None = None,
) -> dict:
    intent = case["intent"]
    catalogue = {row["id"]: row for row in policies}
    policy_ids = list(POLICIES_BY_INTENT[intent])
    if intent == "cancel":
        policy_ids.append("POL04" if order["status"] == "SHIPPED" else "POL03")

    def result(status: str, reason: str, *, missing=(), actions=(), reason_code=None) -> dict:
        return {
            "status": status,
            "reason": reason,
            "reason_code": reason_code,
            "policy_ids_by_action": {
                action: (
                    sorted(set(policy_ids) & {"POL01", "POL13"})
                    if action == "REPLACE"
                    else sorted(policy_ids)
                )
                for action in actions
            },
            "policy_ids": [key for key in policy_ids if key in catalogue],
            "missing_materials": list(missing),
            "available_cents": (
                sum(row["available_cents"] for row in balances)
                if all(row["available_cents"] is not None for row in balances)
                else None
            ),
            "lines": balances,
            "actions": list(actions),
        }

    if policy_blockers:
        return result(
            "HUMAN_REVIEW",
            "当前订单行适用政策需人工核对。",
            reason_code="UNRESOLVED_POLICY",
        )
    if not set(policy_ids) <= catalogue.keys():
        return result(
            "HUMAN_REVIEW", "当前没有足以判定此事项的有效政策。", reason_code="POLICY_REQUIRED"
        )
    if intent == "refund_status":
        return result("STATUS_ONLY", "按原执行记录查询，未知结果仍待核对。")

    verified_payment = (
        order.get("payment_status")
        == catalogue.get("POL02", {})
        .get("parameters", {})
        .get("required_payment_status", "CONFIRMED")
        and parse_time(order.get("paid_at")) is not None
        and type(order.get("paid_cents")) is int
        and order["paid_cents"] > 0
    )
    if intent != "warranty" and not verified_payment:
        return result("HUMAN_REVIEW", "需要核实付款状态、付款时间和实付金额。")

    if intent == "cancel":
        if order["status"] not in {"PAID", "SHIPPED"}:
            return result("INELIGIBLE", "当前履约状态不能办理取消。")
        if facts.get("fulfillment_order_version") != order["version"]:
            return result("HUMAN_REVIEW", "请重新核对当前订单版本的履约状态。")
        if order["status"] == "SHIPPED":
            if facts.get("interception_status") != "CONFIRMED":
                return result("HUMAN_REVIEW", "已交运订单需要明确的物流拦截成功记录。")
        elif facts.get("fulfillment_status") != "UNSHIPPED":
            return result("HUMAN_REVIEW", "请先复核订单仍未交运。")
        if set(case["line_ids"]) != {row["line_id"] for row in order["items"]}:
            return result("INELIGIBLE", "取消订单需核对全单商品。")

    if intent in {"damaged", "wrong_item", "return", "warranty"}:
        delivered = parse_time(order.get("delivered_at"))
        requested = parse_time(case["created_at"])
        if order["status"] != "DELIVERED" or delivered is None or requested is None:
            return result("HUMAN_REVIEW", "需要可核验的签收和申请时间。")
        elapsed = requested - delivered
        if elapsed < timedelta(0):
            return result("HUMAN_REVIEW", "申请时间早于签收时间，需人工核对。")

    if intent in {"damaged", "wrong_item"}:
        evidence_policy = "POL06" if intent == "damaged" else "POL13"
        required = catalogue[evidence_policy]["parameters"].get("required_photo_kinds")
        if not required or not set(required) <= MATERIAL_KINDS:
            return result("HUMAN_REVIEW", "照片要求无法确认。")
        provided = {row["kind"] for row in materials}
        missing = [kind for kind in required if kind not in provided]
        if missing:
            return result("WAITING_INPUT", "请补充所需照片。", missing=missing)
        if review is None:
            return result(
                "HUMAN_REVIEW", "当前材料需人工确认。", reason_code="MATERIAL_REVIEW_REQUIRED"
            )
        if review["decision"] == "NEEDS_MORE":
            return result(
                "WAITING_INPUT",
                "材料审核需要补充，请查看审核意见。",
                reason_code="MATERIALS_NEED_MORE",
            )
        if review["decision"] == "REJECTED":
            return result("INELIGIBLE", "当前版本材料未获人工确认。")

    if intent in {"return", "warranty"}:
        products = {row["id"]: row for row in order["products"]}
        hours = 0
        if intent == "return":
            hours = catalogue["POL05"]["parameters"].get("window_hours")
            if type(hours) is not int or hours < 0:
                return result("HUMAN_REVIEW", "普通退货时限无法确认。")
        checked = []
        for row in balances:
            product = products.get(row["product_id"])
            if product is None:
                line_status = "PRODUCT_UNKNOWN"
            elif intent == "warranty":
                days = product.get("warranty_days")
                if type(days) is not int or days < 0:
                    line_status = "WARRANTY_PERIOD_UNKNOWN"
                elif days > 0 and (elapsed.days < days or elapsed == timedelta(days=days)):
                    line_status = "COVERED"
                else:
                    line_status = "OUTSIDE_WARRANTY"
            elif product.get("returnable") is not True or elapsed > timedelta(hours=hours):
                line_status = "RETURN_NOT_ELIGIBLE"
            else:
                return_fact = facts.get("return_lines", {}).get(row["line_id"], {})
                line_status = (
                    "RETURN_VERIFIED"
                    if return_fact.get("return_receipt_status") == "RECEIVED"
                    and return_fact.get("inspection_status") == "PASSED"
                    else "RETURN_VERIFICATION_REQUIRED"
                )
            checked.append(
                {
                    **row,
                    "rule_status": line_status,
                    "available_cents": row["available_cents"]
                    if line_status == "RETURN_VERIFIED"
                    else 0,
                }
            )
        balances = checked
        states = {row["rule_status"] for row in balances}
        if "PRODUCT_UNKNOWN" in states:
            return result("HUMAN_REVIEW", "订单行商品信息不完整。")
        if intent == "warranty":
            if "WARRANTY_PERIOD_UNKNOWN" in states:
                return result(
                    "HUMAN_REVIEW",
                    "当前订单行质保期限无法确认，请人工核对。",
                    reason_code="WARRANTY_PERIOD_UNKNOWN",
                )
            if "COVERED" in states:
                return result("HUMAN_REVIEW", "符合质保时间条件的商品可提交质保服务审核。")
            return result("INELIGIBLE", "申请商品未覆盖有效质保期限。")
        if "RETURN_VERIFIED" not in states:
            if "RETURN_VERIFICATION_REQUIRED" in states:
                return result("HUMAN_REVIEW", "需先确认对应退件已收到且验货通过。")
            return result("INELIGIBLE", "申请超出普通退货时限或商品不支持普通退货。")

    actions = []
    if ACTION_POLICY_IDS["REFUND"] <= catalogue.keys() and any(
        (row["available_cents"] or 0) > 0 for row in balances
    ):
        actions.append("REFUND")
        policy_ids = sorted(set(policy_ids) | ACTION_POLICY_IDS["REFUND"])
    if intent == "wrong_item" and any(
        (row["replace_available_quantity"] or 0) > 0 for row in balances
    ):
        actions.append("REPLACE")
    if not actions:
        return result("INELIGIBLE", "当前没有可用的退款额度或换发库存。")
    return result("ELIGIBLE", "可以提交当前版本的人工审批方案。", actions=actions)
