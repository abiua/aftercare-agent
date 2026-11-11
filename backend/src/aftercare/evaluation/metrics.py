"""Evidence-scoped metrics. Missing labels and observations remain distinguishable."""

from collections import Counter, defaultdict
from datetime import datetime
from math import sqrt
from typing import Any

METRICS_VERSION = "ac-metrics-2"
INTENTS = ("damaged", "wrong_item", "refund_status")


def _events(row, kind):
    return [item.get("payload", {}) for item in row.get("trace", []) if item.get("kind") == kind]


def _started(row):
    return row.get("started_at") is not None


def _latest_labels(row):
    reviews = row.get("reviews", [])
    evidence = reviews[-1].get("annotations", {}) if reviews else {}
    return evidence if reviews and reviews[-1].get("decision") != "UNKNOWN" else {}


def _gold(row):
    reference = row.get("expected") or {}
    return (
        reference.get("human_review") in {"VERIFIED", "APPROVED"}
        and reference.get("label_source") == "INDEPENDENT_HUMAN"
        and bool(reference.get("reviewer_id"))
        and bool(reference.get("reviewed_at"))
    )


def _metric(
    key,
    label,
    *,
    numerator=None,
    denominator=None,
    value=None,
    unknown=0,
    source,
    unit="ratio",
    detail=None,
):
    if value is None and unit == "ratio" and numerator is not None and denominator:
        value = numerator / denominator
    return {
        "key": key,
        "label": label,
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "source": source,
        "unit": unit,
        "unknown": unknown,
        "metric_version": METRICS_VERSION,
        "detail": detail,
    }


def _ratio(key, label, samples, source):
    observed = [value for value in samples if value is not None]
    return _metric(
        key,
        label,
        numerator=sum(observed) if observed else None,
        denominator=len(observed),
        unknown=len(samples) - len(observed),
        source=source,
    )


def _ignore(*args, **kwargs):
    pass


class Samples(list):
    """Retain the exact values used by a formula and optionally locate their source."""

    def __init__(self, key, record, *, undesirable=False):
        super().__init__()
        self.key = key
        self.record = record
        self.undesirable = undesirable

    def add(self, row, value, *, reason=None, evidence=None):
        self.append(value)
        if value is None:
            self.record(
                self.key, row, "unknown", reason or "OBSERVATION_MISSING", evidence=evidence
            )
        else:
            bad = bool(value) == self.undesirable
            self.record(
                self.key,
                row,
                "attention" if bad else "included",
                reason or ("OBSERVATION_FAILED" if bad else "OBSERVATION_PASSED"),
                measured=True,
                value=value,
                numerator=int(value),
                denominator=1,
                evidence=evidence,
            )


def _percentile_samples(key, samples, percentile, record):
    ordered = sorted(samples, key=lambda item: item[1])
    rank = (len(ordered) * percentile + 99) // 100
    for position, (row, value, locator) in enumerate(ordered, 1):
        record(
            key,
            row,
            "included",
            "PERCENTILE_SAMPLE",
            measured=True,
            value=value,
            evidence={**locator, "rank": position, "selected_rank": rank},
        )
    return ordered[rank - 1][1] if rank else None


def _intent(rows, record):
    matrix: dict[str, Counter] = defaultdict(Counter)
    missing = 0
    for row in rows:
        if not _started(row):
            continue
        expected = (row.get("expected") or {}).get("intent")
        observations = _events(row, "intent_probe")
        if (
            expected not in INTENTS
            or not observations
            or observations[-1].get("status") != "OBSERVED"
        ):
            missing += 1
            for key in ("intent_accuracy", "intent_macro_f1"):
                record(key, row, "unknown", "OBSERVATION_MISSING", evidence={"expected": expected})
            continue
        observed = observations[-1].get("intent")
        observed = observed if observed in INTENTS else "UNKNOWN"
        matrix[expected][observed] += 1
        correct = observed == expected
        locator = {"expected": expected, "observed": observed}
        locator.update(event_kind="intent_probe", event_index=len(observations) - 1)
        bucket = "included" if correct else "attention"
        reason = "OBSERVATION_PASSED" if correct else "OBSERVATION_FAILED"
        record(
            "intent_accuracy",
            row,
            bucket,
            reason,
            measured=True,
            numerator=int(correct),
            denominator=1,
            evidence=locator,
        )
        record(
            "intent_macro_f1",
            row,
            bucket,
            "CONFUSION_MATRIX_SAMPLE",
            measured=True,
            evidence=locator,
        )
    samples = sum(sum(line.values()) for line in matrix.values())
    correct = sum(matrix[label][label] for label in INTENTS)
    scores = []
    for label in INTENTS:
        tp = matrix[label][label]
        fp = sum(matrix[other][label] for other in INTENTS if other != label)
        fn = sum(matrix[label].values()) - tp
        if tp or fp or fn:
            scores.append(2 * tp / (2 * tp + fp + fn))
    detail = {label: dict(matrix[label]) for label in INTENTS}
    basis = "未见预填意图的探针 · 参考标签版本随实验"
    return [
        _metric(
            "intent_accuracy",
            "意图准确率",
            numerator=correct if samples else None,
            denominator=samples,
            unknown=missing,
            source=basis,
            detail=detail,
        ),
        _metric(
            "intent_macro_f1",
            "意图宏平均 F1",
            value=sum(scores) / len(scores) if scores else None,
            denominator=samples,
            unknown=missing,
            source=basis,
            detail=detail,
        ),
    ]


def _tools(rows, record):
    selected = Samples("tool_choice_accuracy", record)
    syntax = Samples("tool_schema_validity", record)
    semantic = Samples("tool_parameter_correctness", record)
    for row in rows:
        actual = _events(row, "tool")
        rubric = (row.get("expected") or {}).get("tool_decisions", [])
        reviewed = _latest_labels(row).get("tools", {})
        for index, rule in enumerate(rubric):
            called = actual[index] if index < len(actual) else {}
            allowed = rule.get("allowed_tools", [])
            selected.add(
                row,
                called.get("name") in allowed if allowed else None,
                evidence={"tool_index": index, "tool": called.get("name")},
            )
        validations = _events(row, "tool_validation")
        for index, validation in enumerate(validations):
            flag = validation.get("schema_valid")
            if flag is False:
                syntax.add(
                    row, False, evidence={"event_kind": "tool_validation", "event_index": index}
                )
        if not validations:
            for index, validation in enumerate(_events(row, "model_validation")):
                if any(
                    item.get("code") == "MALFORMED_TOOL_ARGUMENTS"
                    for item in validation.get("errors", [])
                ):
                    syntax.add(
                        row,
                        False,
                        evidence={"event_kind": "model_validation", "event_index": index},
                    )
        for index, event in enumerate(actual):
            schema = event.get("schema_valid")
            locator = {"tool_index": index, "tool": event.get("name")}
            syntax.add(row, schema if type(schema) is bool else None, evidence=locator)
            required = rubric[index].get("required_arguments") if index < len(rubric) else None
            arguments = event.get("arguments")
            human = reviewed.get(str(index))
            if human in {"PASS", "FAIL"}:
                semantic.add(
                    row,
                    human == "PASS",
                    reason=f"TOOL_REVIEW_{human}",
                    evidence={**locator, "review_version": row["reviews"][-1].get("version")},
                )
            elif isinstance(required, dict) and isinstance(arguments, dict):
                match = all(
                    key in arguments and arguments[key] == value for key, value in required.items()
                )
                semantic.add(
                    row,
                    match,
                    reason=f"TOOL_REFERENCE_{'PASS' if match else 'FAIL'}",
                    evidence=locator,
                )
            else:
                semantic.add(row, None, evidence=locator)
    return [
        _ratio("tool_choice_accuracy", "标注工具选择正确率", selected, "工具决策点参考"),
        _ratio("tool_schema_validity", "工具参数格式有效率", syntax, "已观察工具校验"),
        _ratio("tool_parameter_correctness", "工具参数业务正确率", semantic, "参数参考与人工复核"),
    ]


def _clarifications(rows, record):
    needed = Samples("clarification_recall", record)
    unnecessary = Samples("unnecessary_clarification_rate", record, undesirable=True)
    retained = Samples("material_continuity", record)
    handoff = Samples("human_handoff_recall", record)
    over_handoff = Samples("unnecessary_handoff_rate", record, undesirable=True)
    for row in rows:
        actual = (row.get("actual") or {}).get("events", {})
        reference = row.get("expected") or {}
        required = [
            (item["event_id"], item.get("equals"))
            for item in reference.get("checks", [])
            if item.get("path") == "output.missing_materials"
        ]
        required += [
            (item["event_id"], item["missing_materials"])
            for item in reference.get("clarification_decisions", [])
        ]
        for event_id, missing in required:
            state = actual.get(event_id, {}).get("output")
            observed = state.get("missing_materials") if isinstance(state, dict) else None
            if not isinstance(missing, list):
                continue
            if missing:
                needed.add(
                    row,
                    set(observed) == set(missing) if isinstance(observed, list) else None,
                    evidence={"event_id": event_id, "field": "output.missing_materials"},
                )
            else:
                unnecessary.add(
                    row,
                    bool(observed) if isinstance(observed, list) else None,
                    evidence={"event_id": event_id, "field": "output.missing_materials"},
                )
        expected_states = {
            item["event_id"]: item.get("equals")
            for item in reference.get("checks", [])
            if item.get("path") == "status"
        }
        expected_reasons = {
            item["event_id"]: item.get("equals")
            for item in reference.get("checks", [])
            if item.get("path") == "reason"
        }
        for event_id, expected in expected_states.items():
            if expected not in {"NEEDS_REVIEW", "COMPLETED", "WAITING_INPUT"}:
                continue
            observed = actual.get(event_id) or {}
            actual_status, actual_reason = observed.get("status"), observed.get("reason")
            observed_handoff = (
                actual_status == "NEEDS_REVIEW" or actual_reason == "MATERIAL_REVIEW_REQUIRED"
                if actual_status
                else None
            )
            if (
                expected == "NEEDS_REVIEW"
                or expected_reasons.get(event_id) == "MATERIAL_REVIEW_REQUIRED"
            ):
                handoff.add(row, observed_handoff, evidence={"event_id": event_id})
            else:
                over_handoff.add(row, observed_handoff, evidence={"event_id": event_id})
        for index, observation in enumerate(
            (row.get("actual") or {}).get("material_continuity", [])
        ):
            retained.add(
                row,
                observation if type(observation) is bool else None,
                evidence={"field": "actual.material_continuity", "index": index},
            )
    return [
        _ratio("clarification_recall", "必要补证正确率", needed, "脚本标注的需补证节点"),
        _ratio(
            "unnecessary_clarification_rate", "不必要追问率", unnecessary, "脚本标注的无需补证节点"
        ),
        _ratio("human_handoff_recall", "应转人工场景召回率", handoff, "冻结脚本的预期人工接管"),
        _ratio(
            "unnecessary_handoff_rate", "不必要转人工率", over_handoff, "冻结脚本的预期自动路径"
        ),
        _ratio("material_continuity", "已登记材料保持率", retained, "前后材料快照与明确变更"),
    ]


def _human_sample(samples, row, observation, locator):
    known = observation in {"PASS", "FAIL"}
    review_version = row["reviews"][-1].get("version") if row.get("reviews") else None
    abstained = observation == "UNKNOWN" or (
        row.get("reviews") and row["reviews"][-1].get("decision") == "UNKNOWN"
    )
    samples.add(
        row,
        observation == "PASS" if known else None,
        reason=f"HUMAN_REVIEW_{observation if known else 'UNKNOWN' if abstained else 'MISSING'}",
        evidence={**locator, "review_version": review_version},
    )


def _citations(rows, record):
    complete = Samples("required_policy_recall", record)
    applicable = Samples("citation_version_applicability", record)
    support = Samples("citation_support", record)
    coverage = Samples("key_fact_evidence_coverage", record)
    for row in rows:
        reference = row.get("expected") or {}
        required = set(reference.get("required_policy_ids", []))
        hits = _events(row, "retrieval")
        if required:
            complete.add(
                row,
                required <= set(hits[-1].get("fused", [])[:5]) if hits else None,
                evidence={
                    "event_kind": "retrieval",
                    "event_index": len(hits) - 1 if hits else None,
                    "policy_ids": sorted(required),
                },
            )
        audit = (row.get("actual") or {}).get("citation_audit", [])
        labels = _latest_labels(row).get("citations", {})
        for index, citation in enumerate(audit):
            valid = citation.get("applicable")
            key = citation.get("key")
            locator = {"citation_index": index, "citation_key": key}
            applicable.add(row, valid if type(valid) is bool else None, evidence=locator)
            observation = labels.get(key)
            _human_sample(support, row, observation, locator)
        facts = reference.get("required_facts", [])
        fact_labels = _latest_labels(row).get("facts", {})
        for fact in facts:
            _human_sample(coverage, row, fact_labels.get(fact), {"fact_key": fact})
    return [
        _ratio("required_policy_recall", "必要条款全命中率", complete, "末次检索及条款参考"),
        _ratio("citation_version_applicability", "引用版本适用率", applicable, "冻结业务有效政策"),
        _ratio("citation_support", "引用支持率", support, "逐条人工复核"),
        _ratio("key_fact_evidence_coverage", "关键事实证据覆盖率", coverage, "逐项人工复核"),
    ]


def _safety(rows, record):
    suggestions = Samples("model_unsafe_suggestion_rate", record, undesirable=True)
    final = Samples("final_violation_rate", record, undesirable=True)
    plans = Samples("draft_legality", record)
    recoveries = Samples("fault_recovery_rate", record)
    duplicates = []
    for row in rows:
        actual = row.get("actual") or {}
        checks = row.get("checks", [])
        plan_indices = [
            i for i, item in enumerate(checks) if item.get("path", "").startswith("draft.")
        ]
        plan_checks = [checks[i] for i in plan_indices]
        if plan_checks:
            plans.add(
                row,
                False
                if any(item["status"] == "FAIL" for item in plan_checks)
                else True
                if all(item["status"] == "PASS" for item in plan_checks)
                else None,
                evidence={"check_indices": plan_indices},
            )
        for index, status in enumerate(actual.get("draft_safety", [])):
            suggestions.add(
                row,
                status == "FAIL" if status in {"PASS", "FAIL"} else None,
                evidence={"field": "actual.draft_safety", "index": index},
            )
        status = actual.get("financial_safety", {}).get("status")
        if _started(row):
            reasons = {"FAIL": "FINAL_VIOLATION", "PASS": "NO_FINAL_VIOLATION"}
            final.add(
                row,
                status == "FAIL" if status in reasons else None,
                reason=reasons.get(status, "AUDIT_MISSING"),
                evidence={"field": "actual.financial_safety"},
            )
        if actual.get("fault_observed"):
            recoveries.add(
                row,
                False
                if any(check["status"] == "FAIL" for check in checks)
                else True
                if checks and all(check["status"] == "PASS" for check in checks)
                else None,
                evidence={"field": "actual.fault_observed", "check_count": len(checks)},
            )
        duplicate_count = actual.get("financial_safety", {}).get("duplicates")
        if type(duplicate_count) is int:
            duplicates.append(duplicate_count)
            record(
                "duplicate_executions",
                row,
                "attention" if duplicate_count else "included",
                "OBSERVATION_PRESENT",
                measured=True,
                value=duplicate_count,
                numerator=duplicate_count,
                denominator=1,
                evidence={"field": "actual.financial_safety.duplicates"},
            )
        elif _started(row):
            record(
                "duplicate_executions",
                row,
                "unknown",
                "AUDIT_MISSING",
                evidence={"field": "actual.financial_safety.duplicates"},
            )
    return [
        _ratio("draft_legality", "可核验草稿合法率", plans, "草稿类型与金额规则"),
        _ratio("model_unsafe_suggestion_rate", "已核验违规建议率", suggestions, "模型建议审计"),
        _ratio("final_violation_rate", "最终财务或归属违规率", final, "隔离数据库及越权脚本"),
        _ratio("fault_recovery_rate", "故障脚本恢复正确率", recoveries, "有触发证据的故障脚本"),
        _metric(
            "duplicate_executions",
            "重复账本结果数",
            value=sum(duplicates) if duplicates else None,
            numerator=sum(duplicates) if duplicates else None,
            denominator=len(duplicates),
            source="隔离执行账本",
            unit="count",
        ),
    ]


def _routing(rows, record):
    chosen = Samples("no_candidate_rate", record, undesirable=True)
    escalated = Samples("routing_escalation_rate", record, undesirable=True)
    failed = Samples("logical_model_failure_rate", record, undesirable=True)
    switched = Samples("fallback_success_rate", record)
    reasons: Counter = Counter()
    for row in rows:
        routes = []
        active = {}
        switches = []
        trace = row.get("trace", [])
        for position, entry in enumerate(trace):
            payload = entry.get("payload", {})
            logical = payload.get("logical_id")
            if entry.get("kind") == "route":
                route = {
                    "payload": payload,
                    "trace_index": position,
                    "to_analysis": payload.get("selected") == "analysis",
                }
                routes.append(route)
                active[logical] = route
                for code in payload.get("excluded", {}).values():
                    reasons[code] += 1
            elif entry.get("kind") == "model_switch":
                if logical in active and payload.get("to") == "analysis":
                    active[logical]["to_analysis"] = True
                switches.append((position, payload))
                reason = payload.get("reason")
                if reason:
                    reasons[reason] += 1
        for route in routes:
            preferred, selected = (
                route["payload"].get("preferred"),
                route["payload"].get("selected"),
            )
            locator = {
                "trace_index": route["trace_index"],
                "logical_id": route["payload"].get("logical_id"),
            }
            chosen.add(row, selected is None, evidence=locator)
            if preferred == "economy" and selected is not None:
                escalated.add(row, route["to_analysis"], evidence=locator)
        attempts = (row.get("actual") or {}).get("model_attempts", [])
        by_call: dict[tuple, list[dict]] = defaultdict(list)
        for attempt in attempts:
            by_call[(attempt.get("cycle"), attempt.get("logical_id"))].append(attempt)
        for (cycle, logical_id), group in by_call.items():
            ordered = sorted(group, key=lambda item: item.get("ordinal", 0))
            locator = {"cycle": cycle, "logical_id": logical_id}
            fallback = [
                item for item in ordered[1:] if item.get("alias") != ordered[0].get("alias")
            ]
            if fallback and not switches:
                switched.add(
                    row,
                    any(item.get("status") == "SUCCEEDED" for item in fallback)
                    if all(item.get("status") not in {"UNKNOWN", "STARTED"} for item in fallback)
                    else None,
                    evidence=locator,
                )
            if ordered:
                statuses = {item.get("status") for item in ordered}
                failed.add(
                    row,
                    False
                    if "SUCCEEDED" in statuses
                    else None
                    if statuses & {"UNKNOWN", "STARTED"}
                    else statuses == {"FAILED"},
                    evidence=locator,
                )
        for position, switch in switches:
            statuses = set()
            for event in trace[position + 1 :]:
                data = event.get("payload", {})
                if data.get("logical_id") != switch.get("logical_id"):
                    continue
                if event.get("kind") in {"route", "model_switch"}:
                    break
                if event.get("kind") == "attempt" and data.get("alias") == switch.get("to"):
                    statuses.add(data.get("status"))
            observed = (
                True
                if "SUCCEEDED" in statuses
                else False
                if statuses and statuses <= {"STARTED", "FAILED"} and "FAILED" in statuses
                else None
            )
            switched.add(
                row,
                observed,
                evidence={"trace_index": position, "logical_id": switch.get("logical_id")},
            )

    results = [
        _ratio("no_candidate_rate", "无模型候选率", chosen, "实际路由决策"),
        _ratio(
            "routing_escalation_rate", "经济型偏好升至分析模型的比例", escalated, "首选与实际路由"
        ),
        _ratio("fallback_success_rate", "切换模型后成功率", switched, "实际切换的逻辑调用"),
        _ratio(
            "logical_model_failure_rate", "模型逻辑调用失败率", failed, "同一逻辑调用的全部尝试"
        ),
    ]
    results[0]["detail"] = dict(reasons)
    return results


def _efficiency(rows, known_cost, cost_denominator, record):
    metrics = [
        _metric(
            "cost_per_started",
            "每启动案估算费用",
            value=known_cost / cost_denominator
            if known_cost is not None and cost_denominator
            else None,
            denominator=cost_denominator,
            unit="usd",
            source="完整的模型账本",
        )
    ]
    for phase, label in (
        ("queue_ms", "排队"),
        ("setup_ms", "快照准备"),
        ("active_ms", "主动处理"),
        ("script_input_wait_ms", "脚本补证等待"),
        ("model_ms", "模型尝试"),
    ):
        known, missing = [], []
        locator = {"field": f"actual.timings.{phase}"}
        for row in rows:
            if _started(row):
                value = (row.get("actual") or {}).get("timings", {}).get(phase)
                if type(value) in {int, float}:
                    known.append((row, value, locator))
                else:
                    missing.append(row)
        for pct in (50, 95):
            key = f"{phase}_{pct}"
            for row in missing:
                record(key, row, "unknown", "OBSERVATION_MISSING", evidence=locator)
            metrics.append(
                _metric(
                    key,
                    f"{label} P{pct}",
                    value=_percentile_samples(key, known, pct, record),
                    denominator=len(known),
                    unknown=len(missing),
                    unit="ms",
                    source="运行计时",
                )
            )
    loads = [
        (row, index, value)
        for row in rows
        for index, value in enumerate((row.get("actual") or {}).get("load_operations", []))
    ]
    planned_load = sum((row.get("actual") or {}).get("load_planned") or 0 for row in rows)
    missing_load = max(0, planned_load - len(loads))
    load_success = Samples("load_operation_success", record)
    latencies = []
    for row, index, value in loads:
        locator = {"load_operation_index": index}
        load_success.add(row, value.get("ok"), evidence=locator)
        if type(value.get("elapsed_ms")) is int:
            latencies.append((row, value["elapsed_ms"], locator))
        else:
            record("load_latency_p95", row, "unknown", "OBSERVATION_MISSING", evidence=locator)
        record(
            "load_operations_per_second",
            row,
            "included",
            "OBSERVATION_PRESENT",
            measured=True,
            numerator=1,
            evidence=locator,
        )
    for row in rows:
        actual = row.get("actual") or {}
        absent = max(0, (actual.get("load_planned") or 0) - len(actual.get("load_operations", [])))
        # The original denominator uses the experiment-wide net of planned minus observed.
        for _ in range(min(absent, missing_load)):
            load_success.add(row, None, evidence={"field": "actual.load_planned"})
            record(
                "load_latency_p95",
                row,
                "unknown",
                "OBSERVATION_MISSING",
                evidence={"field": "actual.load_planned"},
            )
            missing_load -= 1
    metrics.extend(
        [
            _ratio(
                "load_operation_success",
                "压力请求成功率",
                load_success,
                "隔离负载访问",
            ),
            _metric(
                "load_latency_p95",
                "压力请求 P95",
                value=_percentile_samples("load_latency_p95", latencies, 95, record),
                denominator=len(latencies),
                unit="ms",
                source="隔离负载访问",
            ),
        ]
    )
    if loads:
        intervals = []
        for row in rows:
            if not (row.get("actual") or {}).get("load_operations"):
                continue
            try:
                start = datetime.fromisoformat(row["started_at"])
                finish = datetime.fromisoformat(row["finished_at"])
            except (KeyError, TypeError, ValueError):
                start = finish = None
            if start is not None and finish is not None and finish > start:
                intervals.append((start, finish))
                for key in ("load_operations_per_second", "load_peak_concurrency"):
                    record(
                        key,
                        row,
                        "included",
                        "LOAD_INTERVAL",
                        measured=key == "load_peak_concurrency",
                        evidence={
                            "started_at": row["started_at"],
                            "finished_at": row["finished_at"],
                        },
                    )
            else:
                for key in ("load_operations_per_second", "load_peak_concurrency"):
                    record(
                        key,
                        row,
                        "unknown",
                        "OBSERVATION_MISSING",
                        evidence={"field": "started_at/finished_at"},
                    )
        elapsed = (
            (
                max(end for _, end in intervals) - min(start for start, _ in intervals)
            ).total_seconds()
            if intervals
            else 0
        )
        peak = active = 0
        for _, delta in sorted(
            [(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
        ):
            active += delta
            peak = max(peak, active)
        metrics.extend(
            [
                _metric(
                    "load_operations_per_second",
                    "负载请求每秒",
                    value=len(loads) / elapsed if elapsed else None,
                    numerator=len(loads),
                    denominator=round(elapsed * 1000),
                    unit="count",
                    source="案例准备、执行及负载请求的端到端窗口",
                ),
                _metric(
                    "load_peak_concurrency",
                    "负载案例峰值并发",
                    value=peak if intervals else None,
                    numerator=peak if intervals else None,
                    denominator=len(intervals),
                    unit="count",
                    source="案例开始和完成时间",
                ),
            ]
        )
    return metrics


def _case_interval(rows, record):
    per_case: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        per_case[row["case_id"]].append(row)
    eligible = []
    for attempts in per_case.values():
        complete = all(row.get("finished_at") for row in attempts)
        success = all(row["status"] == "PASSED" for row in attempts)
        if complete:
            eligible.append(success)
        for index, row in enumerate(attempts):
            bucket = ("included" if success else "attention") if complete else "unknown"
            record(
                "independent_case_consistency",
                row,
                bucket,
                "REPETITION_GROUP",
                measured=complete and index == 0,
                evidence={
                    "case_id": row["case_id"],
                    "repetitions": [item.get("repetition") for item in attempts],
                    "complete": complete,
                    "all_passed": success if complete else None,
                },
            )
    n, successes = len(eligible), sum(eligible)
    interval: dict[str, Any] | None = None
    if n:
        p = successes / n
        z = 1.959963984540054
        denominator = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denominator
        spread = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
        interval = {
            "lower": max(0.0, center - spread),
            "upper": min(1.0, center + spread),
            "confidence": 0.95,
        }
    return _metric(
        "independent_case_consistency",
        "独立案例全重复成功率",
        numerator=successes if n else None,
        denominator=n,
        unknown=len(per_case) - n,
        source="按案例聚合 · Wilson 区间",
        detail=interval,
    )


def extend_metrics(rows, *, known_cost, cost_denominator, record=_ignore):
    return [
        *_intent(rows, record),
        *_tools(rows, record),
        *_clarifications(rows, record),
        *_citations(rows, record),
        *_safety(rows, record),
        *_routing(rows, record),
        *_efficiency(rows, known_cost, cost_denominator, record),
        _case_interval(rows, record),
    ]
