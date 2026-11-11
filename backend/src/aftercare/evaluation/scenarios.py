"""Scenario scopes and paginated evidence from the evaluation metric calculators."""

from collections import Counter, defaultdict

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest

from .metrics import METRICS_VERSION

UNREGISTERED = "__unregistered__"
BUCKETS = ("included", "attention", "unknown", "excluded")
INTENT_LABELS = {"damaged": "破损售后", "wrong_item": "错发售后", "refund_status": "退款进度"}
SCENARIO_LABELS = {
    UNREGISTERED: "历史报告未登记场景",
    "D_OPEN_BOX": "开箱发现破损",
    "D_ITEM_ONLY": "破损仅有商品照片",
    "D_REVIEW_REJECTED": "破损材料审核未通过",
    "D_NO_DELIVERY_TIME": "破损订单缺签收时间",
    "D_REVIEW_ASKS_MORE": "破损审核要求补证",
    "D_SELECTED_LINE_EXHAUSTED": "所选订单行退款额度用尽",
    "D_PAYMENT_UNCONFIRMED": "破损订单支付未确认",
    "D_REVIEW_VERSION_CHANGED": "破损材料版本变化",
    "W_NO_PHOTOS": "错发缺两类照片",
    "W_ITEM_WITHOUT_LABEL": "错发缺包裹标签",
    "W_LABEL_THEN_REVIEW": "补包裹标签后审核",
    "W_REVIEW_REJECTED": "错发材料审核未通过",
    "W_NO_STOCK_WITH_BALANCE": "换发缺库存且仍有额度",
    "W_NO_STOCK_NO_BALANCE": "换发缺库存且无额度",
    "W_PAYMENT_NOT_VERIFIED": "错发订单支付未核实",
    "W_STOCK_APPROVED": "错发材料通过且有换发库存",
    "R_COMPLETED": "退款账本显示成功",
    "R_CUSTOMER_CLAIM_CONFLICT": "客户退款说法与账本有出入",
    "R_FAILED": "退款账本显示失败",
    "R_MIXED_RECORDS": "同订单多条退款记录",
    "R_NO_LEDGER": "订单未登记退款执行",
    "R_SUBMITTING": "退款已经登记提交",
    "R_UNDATED_HISTORICAL_BALANCE": "历史退款金额缺登记日期",
    "R_UNKNOWN": "退款执行结果未知",
    "AGENT_CRASH_PENDING_ATTEMPT": "助手进程中断且调用待确认",
    "CONCURRENT_EXECUTION": "并发执行",
    "CONFLICTING_POLICY_VERSION": "政策版本冲突",
    "CROSS_KIND_REUSED_IMAGE": "同一照片重复用于不同材料",
    "CUSTOMER_PAYMENT_CLAIM": "客户支付说法待核对",
    "DELIVERY_TIMESTAMP_UNKNOWN": "缺少物流签收时间",
    "DUPLICATE_MATTER": "同类售后目标重复",
    "EXECUTION_IDEMPOTENCY": "执行请求重复提交",
    "EXECUTION_WITHOUT_APPROVAL": "无有效审批发起执行",
    "EXPIRED_APPROVAL": "审批过期",
    "FUTURE_POLICY": "政策尚未生效",
    "GROUPED_STOCK_CAP": "同商品库存总量限制",
    "INVALID_IMAGE": "材料照片无效",
    "LINE_REFUND_CAP": "订单行退款上限",
    "OWNER_SCOPE": "客户访问归属边界",
    "PAYMENT_COMMITTED_THEN_CRASH": "支付已登记后执行进程中断",
    "PAYMENT_OUTCOME_UNKNOWN": "支付调用结果无法确认",
    "PENDING_REFUND_RESERVATION": "待确认退款占用额度",
    "RESUME_WITHOUT_EVIDENCE": "未补充材料即请求继续",
    "REVIEW_WITH_MISSING_MATERIAL": "材料不全时提交审核",
    "ROLE_APPROVAL": "审批角色权限",
    "STALE_MATERIAL_BINDING": "审批所绑定的材料已变更",
    "UNAPPROVED_PARTIAL_REFUND": "部分退款缺少事实审核",
    "ZERO_REPLACEMENT_STOCK": "换发库存为零",
}

# Sample count units are separate from a metric's displayed value and denominator.
METRIC_UNITS = {
    "check_pass": "check",
    "check_fail": "check",
    "check_unknown": "check",
    "model_attempts": "model_attempt",
    "cost_unknown": "model_attempt",
    "cost": "model_attempt",
    "cost_per_success": "run",
    "cost_per_started": "run",
    "recall_at_5": "policy",
    "observed_tool_success": "tool_call",
    "tool_choice_accuracy": "tool_decision",
    "tool_schema_validity": "tool_call",
    "tool_parameter_correctness": "tool_call",
    "clarification_recall": "event",
    "unnecessary_clarification_rate": "event",
    "human_handoff_recall": "event",
    "unnecessary_handoff_rate": "event",
    "material_continuity": "event",
    "citation_version_applicability": "citation",
    "citation_support": "citation",
    "key_fact_evidence_coverage": "fact",
    "model_unsafe_suggestion_rate": "event",
    "fault_recovery_rate": "fault",
    "no_candidate_rate": "route",
    "routing_escalation_rate": "route",
    "fallback_success_rate": "logical_call",
    "logical_model_failure_rate": "logical_call",
    "load_operation_success": "load_operation",
    "load_latency_p95": "load_operation",
    "load_operations_per_second": "load_operation",
    "load_peak_concurrency": "interval",
    "independent_case_consistency": "case",
}
DENOMINATOR_UNITS = {
    "model_attempts": "run",
    "cost": "run",
    "load_operations_per_second": "millisecond",
}
RUN_UNKNOWN = {"model_attempts", "cost", "cost_per_success", "cost_per_started"}


class Evidence:
    def __init__(self):
        self.records = defaultdict(lambda: defaultdict(list))
        self.measured = Counter()
        self.measured_results = defaultdict(set)

    def add(self, key, row, bucket, reason, *, measured=False, **details):
        self.records[key][row["id"]].append(
            {
                "bucket": bucket,
                "reason": reason,
                "measured": measured,
                **{name: value for name, value in details.items() if value is not None},
            }
        )
        if measured:
            self.measured[key] += 1
            self.measured_results[key].add(row["id"])

    def rows(self, key, results):
        samples = self.records[key]
        return [
            {
                "result_id": row["id"],
                "case_id": row["case_id"],
                "repetition": row["repetition"],
                "machine_status": row["machine_status"],
                "effective_status": row["status"],
                "review_version": row["reviews"][-1]["version"] if row["reviews"] else None,
                "contributions": samples.get(row["id"])
                or [{"bucket": "excluded", "reason": "NOT_APPLICABLE"}],
            }
            for row in results
        ]


def _family(row):
    family = (row.get("expected") or {}).get("family")
    return family if isinstance(family, str) and family.strip() else UNREGISTERED


def _options(keys, labels, unknown):
    options = []
    unknown_number = 0
    for key in sorted(set(keys)):
        label = labels.get(key)
        if label is None:
            unknown_number += 1
            label = f"{unknown} {unknown_number}"
        options.append({"key": key, "label": label})
    return options


def scenario_report(
    experiment,
    results,
    count_rows,
    compute_metrics,
    *,
    scenario=None,
    intent=None,
    metric_key=None,
    basis_revision=None,
    bucket=None,
    limit=30,
    offset=0,
):
    scenario_options = _options(map(_family, results), SCENARIO_LABELS, "其他登记场景")
    intent_options = _options(
        (row["intent"] for row in results if row["intent"]), INTENT_LABELS, "其他售后类型"
    )
    scope = {"scenario": scenario, "intent": intent}
    for key, options in (("scenario", scenario_options), ("intent", intent_options)):
        if scope[key] is not None and not any(item["key"] == scope[key] for item in options):
            raise BusinessError("INVALID_METRIC_SCOPE", "此实验没有登记所选场景或售后类型。", 422)
    rows = [
        row
        for row in results
        if (scenario is None or _family(row) == scenario)
        and (intent is None or row["intent"] == intent)
    ]
    config = experiment.config
    config_hash = digest(config)
    revision = digest(
        [
            "ac-scenario-basis-v1",
            METRICS_VERSION,
            experiment.id,
            experiment.status,
            experiment.version,
            config_hash,
            scope,
            rows,
        ]
    )
    frozen = config.get("metrics_version")
    available = frozen == METRICS_VERSION
    counts = count_rows(rows)
    unique_cases = len({row["case_id"] for row in rows})
    payload = {
        "schema_version": "ac-v1",
        "experiment_id": experiment.id,
        "scope": scope,
        "dataset_id": config.get("dataset_id"),
        "config_hash": config_hash,
        "frozen_metrics_version": frozen,
        "computed_metrics_version": METRICS_VERSION,
        "status": "AVAILABLE" if available else "HISTORICAL_DEFINITION_UNAVAILABLE",
        "scenario_options": scenario_options,
        "intent_options": intent_options,
        "scope_counts": {
            **counts,
            "unique_cases": unique_cases,
            "repeated_runs": len(rows) - unique_cases,
        },
        "basis_revision": revision,
    }
    if metric_key is not None and basis_revision != revision:
        raise BusinessError("METRIC_BASIS_CHANGED", "指标口径已更新，请刷新后查看贡献案例。")
    if not available:
        if metric_key is not None:
            raise BusinessError("METRIC_DEFINITION_UNAVAILABLE", "此历史指标定义暂无法下钻。", 422)
        return {**payload, "metrics": []}

    evidence = Evidence()
    computed = compute_metrics(rows, counts, config["source"], evidence=evidence)
    metrics = []
    for metric in computed:
        key = metric["key"]
        units = METRIC_UNITS.get(key, "run")
        measured_rows = evidence.records.get(key, {})
        missing = [
            [
                item
                for item in items
                if item["bucket"] == "unknown" or item["reason"] == "AUDIT_MISSING"
            ]
            for items in measured_rows.values()
        ]
        unknown = (
            sum(bool(items) for items in missing) if key in RUN_UNKNOWN else sum(map(len, missing))
        )
        if key == "independent_case_consistency":
            unknown = metric["unknown"]
        unknown_unit = "run" if key in RUN_UNKNOWN else units
        if key == "load_operations_per_second":
            unknown_unit = "interval"
        metrics.append(
            {
                **metric,
                "metric_version": METRICS_VERSION,
                "sample_unit": units,
                "denominator_unit": DENOMINATOR_UNITS.get(key, units),
                "measured_samples": evidence.measured[key],
                "unique_cases": len(
                    {row["case_id"] for row in rows if row["id"] in evidence.measured_results[key]}
                ),
                "unknown": metric.get("unknown", unknown),
                "evidence_unknown": unknown,
                "unknown_includes_measured": any(
                    item.get("measured") for items in missing for item in items
                ),
                "unknown_unit": unknown_unit,
                "drilldown_supported": key in SUPPORTED_METRICS,
            }
        )
    if metric_key is None:
        return {**payload, "metrics": metrics}

    metric = next((item for item in metrics if item["key"] == metric_key), None)
    if metric is None:
        raise BusinessError("METRIC_NOT_FOUND", "所选范围没有此项指标。", 404)
    if not metric["drilldown_supported"]:
        raise BusinessError("METRIC_DRILLDOWN_UNAVAILABLE", "此指标暂无法定位到单案。", 422)
    items = evidence.rows(metric_key, rows)
    bucket_counts = dict.fromkeys(BUCKETS, 0)
    matched = []
    for item in items:
        categories = {sample["bucket"] for sample in item["contributions"]}
        if any(sample.get("measured") for sample in item["contributions"]):
            categories.add("included")
        for category in categories:
            bucket_counts[category] += 1
        if bucket is None or bucket in categories:
            matched.append(item)
    return {
        "schema_version": "ac-v1",
        "experiment_id": experiment.id,
        "scope": scope,
        "basis_revision": revision,
        "metric": metric,
        "bucket_counts": bucket_counts,
        "items": matched[offset : offset + limit],
        "total": len(matched),
        "limit": limit,
        "offset": offset,
    }


SUPPORTED_METRICS = frozenset(METRIC_UNITS) | {
    "task_success",
    "coverage",
    "model_audit_coverage",
    "latency_p50",
    "latency_p95",
    "draft_rule_blocks",
    "intent_accuracy",
    "intent_macro_f1",
    "required_policy_recall",
    "draft_legality",
    "final_violation_rate",
    "duplicate_executions",
    *(
        f"{phase}_{percent}"
        for phase in ("queue_ms", "setup_ms", "active_ms", "script_input_wait_ms", "model_ms")
        for percent in (50, 95)
    ),
}
