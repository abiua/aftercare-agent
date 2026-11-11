"""Persist the local registration, immutable freeze, and holdout exposure journal."""

import fcntl
import os
import stat
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.config import ROOT

from ..imports import MAX_REPORT_BYTES, ImportInput, parse_report
from .contracts import (
    Candidate,
    ConditionControls,
    Digest,
    ExposureEvent,
    FormalFreezeDraftV2,
    FreezeConfirmation,
    FreezeDraft,
    FreezeQuota,
    HumanRecord,
    Identifier,
    NativeValidation,
    NativeValidationSnapshot,
    PackageManifest,
    ReportSnapshot,
    Unlock,
    Validation,
    ValidationPlan,
    ValidationSnapshot,
    parse_freeze_draft,
)
from .files import (
    MAX_PACKAGE,
    append_event,
    directory,
    fail,
    json_bytes,
    json_data,
    read,
    read_path,
    read_private,
    write_once,
)
from .package import PackageBinding, PrivatePackage, load_private_package, reopen_private_package

DEFAULT_ROOT = ROOT / ".runtime/formal-evaluation"
HUMAN_IDENTITY = "UNAUTHENTICATED_DECLARATION"


class _BoundExposureEvent(ExposureEvent):
    kind: Literal[
        "OPENING", "REOPENING", "CASES_READ", "UNLOCKED", "REOPENED", "EXPOSURE_UNCERTAIN"
    ]
    read_id: UUID
    freeze_sha256: Digest
    package_binding: PackageBinding | None = Field(default=None, repr=False)
    matching_case_fingerprints_sha256: Digest | None = None


@dataclass(frozen=True, slots=True)
class ExecutionHandoff:
    freeze_sha256: str
    package: PrivatePackage = field(repr=False)
    _draft: bytes = field(repr=False)
    _controls: bytes = field(repr=False)

    @property
    def draft(self) -> FormalFreezeDraftV2:
        return parse_freeze_draft(json_data(self._draft))

    @property
    def controls(self) -> dict[str, ConditionControls]:
        return TypeAdapter(dict[str, ConditionControls]).validate_python(json_data(self._controls))


@dataclass
class _PackageRead:
    opening: _BoundExposureEvent
    cases: _BoundExposureEvent | None = None
    terminal: _BoundExposureEvent | None = None


def _hash(raw):
    return sha256(raw).hexdigest()


def _sha(record):
    return _hash(json_bytes(record.model_dump(mode="json")))


def _optional(fd, name):
    try:
        return json_data(read(fd, name))
    except FileNotFoundError:
        return None


def _private(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        fail("FORMAL_PATH_INVALID", "登记目录必须由当前用户拥有，且仅当前用户可访问。")


def _event(fd, kind, human, reason, *, freeze_sha256=None, package=None, case_fingerprints=None):
    item = ExposureEvent(
        kind=kind,
        recorded_at=datetime.now(UTC),
        human=human,
        reason=reason,
        freeze_sha256=freeze_sha256,
        package=package,
        case_fingerprints=case_fingerprints,
    )
    append_event(fd, item.model_dump(mode="json"))


def _events(fd):
    try:
        raw = read_private(fd, "exposure.jsonl")
    except FileNotFoundError:
        return []
    events = []
    try:
        if raw and not raw.endswith(b"\n"):
            raise ValueError("Incomplete journal event")
        for line in raw.splitlines():
            value = json_data(line)
            model = (
                _BoundExposureEvent
                if isinstance(value, dict) and "read_id" in value
                else ExposureEvent
            )
            events.append(model.model_validate(value))
    except (ValueError, RecursionError):
        raise BusinessError("FORMAL_JOURNAL_INVALID", "候选暴露记录格式不符合要求。") from None
    return events


def _bound_event(fd, kind, opening, reason, **payload):
    event = _BoundExposureEvent(
        kind=kind,
        recorded_at=datetime.now(UTC),
        human=opening.human,
        reason=reason,
        read_id=opening.read_id,
        freeze_sha256=opening.freeze_sha256,
        **payload,
    )
    append_event(fd, event.model_dump(mode="json", exclude_none=True))
    return event


def _begin_package_read(fd, kind, human, freeze_sha256):
    event = _BoundExposureEvent(
        kind=kind,
        recorded_at=datetime.now(UTC),
        human=human,
        read_id=uuid4(),
        reason="已登记本次读取，准备核对私有候选包。",
        freeze_sha256=freeze_sha256,
    )
    append_event(fd, event.model_dump(mode="json", exclude_none=True))
    return event


def _journal_require(condition):
    if not condition:
        fail("FORMAL_JOURNAL_INVALID", "候选暴露记录的读取顺序或绑定不一致。")


def _package_reads(events):
    attempts = []
    seen_ids = set()
    original_fingerprints = None
    for event in events:
        if not isinstance(event, _BoundExposureEvent):
            _journal_require(event.kind == "EXPOSED_BEFORE_FREEZE")
            continue
        _journal_require(event.package is None)
        _journal_require(event.package_binding is None or event.kind == "UNLOCKED")
        _journal_require(
            event.kind == "CASES_READ"
            or (event.case_fingerprints is None and event.matching_case_fingerprints_sha256 is None)
        )
        if event.kind in {"OPENING", "REOPENING"}:
            _journal_require(event.read_id not in seen_ids)
            if not attempts:
                _journal_require(event.kind == "OPENING")
            else:
                previous = attempts[-1].terminal
                _journal_require(
                    previous is not None
                    and previous.kind in {"UNLOCKED", "REOPENED"}
                    and event.kind == "REOPENING"
                    and event.freeze_sha256 == previous.freeze_sha256
                )
            seen_ids.add(event.read_id)
            attempts.append(_PackageRead(event))
            continue
        _journal_require(bool(attempts))
        attempt = attempts[-1]
        opening = attempt.opening
        _journal_require(
            event.read_id == opening.read_id
            and event.human == opening.human
            and event.freeze_sha256 == opening.freeze_sha256
            and (
                attempt.terminal is None
                or (
                    event.kind == "EXPOSURE_UNCERTAIN"
                    and attempt.terminal.kind in {"UNLOCKED", "REOPENED"}
                )
            )
        )
        if event.kind == "CASES_READ":
            _journal_require(attempt.cases is None)
            matching = event.matching_case_fingerprints_sha256
            if matching is None:
                fingerprints = event.case_fingerprints
                _journal_require(
                    fingerprints is not None and fingerprints == sorted(set(fingerprints))
                )
            else:
                _journal_require(
                    opening.kind == "REOPENING"
                    and event.case_fingerprints is None
                    and original_fingerprints is not None
                    and matching == digest(original_fingerprints)
                )
            attempt.cases = event
            continue
        if event.kind == "UNLOCKED":
            _journal_require(
                opening.kind == "OPENING"
                and event.package_binding is not None
                and attempt.cases is not None
                and bool(attempt.cases.case_fingerprints)
            )
            original_fingerprints = attempt.cases.case_fingerprints
        if event.kind == "REOPENED":
            _journal_require(
                opening.kind == "REOPENING"
                and attempt.cases is not None
                and attempt.cases.matching_case_fingerprints_sha256 is not None
            )
        attempt.terminal = event
    return attempts


def _candidate(fd):
    value = _optional(fd, "candidate.json")
    if value is None:
        fail("FORMAL_CANDIDATE_REQUIRED", "请先登记留出候选。")
    return Candidate.model_validate(value)


def _old_inputs():
    raw = read_path(ROOT / "data/v0.2/manifest.json")
    manifest = json_data(raw)
    return {_hash(raw), *manifest["artifacts"].values()}


def _verify_pair(group, reports, varied):
    baseline = reports[0]
    for report in reports[1:]:
        common = set(baseline["config"]) | set(report["config"])
        if any(
            baseline["config"].get(key) != report["config"].get(key) for key in common - {varied}
        ):
            fail("FORMAL_VALIDATION_MISMATCH", f"验证组 {group} 改变了控制条件。")
        if _pairs(report) != _pairs(baseline):
            fail("FORMAL_VALIDATION_MISMATCH", f"验证组 {group} 的案例或重复编号不一致。")


def _pairs(report):
    return {(row["case_id"], row["repetition"]) for row in report["results"]}


def _reports(candidate, plan, validation):
    if validation.plan_sha256 != _sha(plan):
        fail("FORMAL_VALIDATION_MISMATCH", "验证报告必须绑定原验证计划摘要。")
    groups = {"A": [], "B": []}
    snapshots = []
    expected_pairs = {
        (case_id, repetition)
        for case_id in plan.cases
        for repetition in range(1, plan.repetitions + 1)
    }
    input_digest = digest({key: case.case_sha256 for key, case in plan.cases.items()})
    reference_digest = digest({key: case.reference_sha256 for key, case in plan.cases.items()})
    for entry in validation.reports:
        raw = read_path(entry.path, maximum=MAX_REPORT_BYTES)
        if _hash(raw) != entry.sha256:
            fail("FORMAL_VALIDATION_CHANGED", "验证报告与登记摘要不一致。")
        json_data(raw)
        report = parse_report(
            ImportInput(content=raw.decode(), sha256=entry.sha256, origin="local")
        )
        config = report["config"]
        source = "live" if candidate.purpose == "formal" else "fixture"
        config_sha = digest(config)
        condition = f"{config.get('mode')}/{config.get('strategy')}"
        if (
            not isinstance(report.get("id"), str)
            or config.get("dataset_id") != plan.dataset_id
            or config.get("split") != "validation"
            or config.get("source") != source
            or config.get("input_digest") != input_digest
            or config.get("reference_digest") != reference_digest
            or plan.config_sha256.get(condition) != config_sha
            or report["status"] != "COMPLETED"
            or _pairs(report) != expected_pairs
            or report["counts"]["finished"] != len(expected_pairs)
        ):
            fail("FORMAL_VALIDATION_MISMATCH", "验证报告未匹配预登记的输入全集、配置或执行覆盖。")
        for row in report["results"]:
            case = plan.cases[row["case_id"]]
            quota, reference = row.get("quota"), row.get("expected")
            if (
                not isinstance(quota, dict)
                or quota.get("case_hash") != case.case_sha256
                or not isinstance(reference, dict)
                or digest(reference) != case.reference_sha256
            ):
                fail("FORMAL_VALIDATION_MISMATCH", "验证结果的案例或参考摘要与计划不同。")
        groups[entry.group].append(report)
        model = config.get("model_snapshot")
        snapshots.append(
            ReportSnapshot(
                group=entry.group,
                experiment_id=report["id"],
                report_sha256=entry.sha256,
                config_sha256=config_sha,
                source=source,
                mode=config["mode"],
                strategy=config["strategy"],
                counts={
                    key: report["counts"][key]
                    for key in ("planned", "started", "finished", "not_run")
                },
                code_digest=config.get("code_digest"),
                scorer_version=config.get("scorer_version"),
                metrics_version=config.get("metrics_version"),
                model_snapshot_digest=digest(model) if model else None,
            )
        )
    _validate_matrix(groups, validation.selected_experiment_id)
    return ValidationSnapshot(
        plan_sha256=validation.plan_sha256,
        reports=snapshots,
        selected_experiment_id=validation.selected_experiment_id,
        rationale=validation.rationale,
        selection=validation.selection,
    )


def _validate_matrix(groups, selected_experiment_id):
    group_a, group_b = groups.values()
    a_by_strategy = {row["config"].get("strategy"): row for row in group_a}
    b_by_mode = {row["config"].get("mode"): row for row in group_b}
    if (
        len(group_a) != 3
        or set(a_by_strategy) != {"economy", "analysis", "routed"}
        or any(row["config"].get("mode") != "single" for row in group_a)
        or len(group_b) != 2
        or set(b_by_mode) != {"single", "multi"}
    ):
        fail(
            "FORMAL_VALIDATION_MISMATCH", "验证矩阵须包含单 Agent 三策略与同策略单／三 Agent 对照。"
        )
    _verify_pair("A", group_a, "strategy")
    _verify_pair("B", group_b, "mode")
    anchor = b_by_mode["single"]
    if anchor != a_by_strategy.get(anchor["config"].get("strategy")):
        fail("FORMAL_VALIDATION_MISMATCH", "两组验证须复用同一固定策略的单 Agent 基线。")
    unique = [row["id"] for row in (*group_a, b_by_mode["multi"])]
    if len(set(unique)) != 4 or selected_experiment_id not in unique:
        fail("FORMAL_VALIDATION_MISMATCH", "入选实验必须唯一对应一份已登记验证报告。")


def _validate_draft(candidate, plan, validation, draft):
    if (
        draft.candidate_sha256 != _sha(candidate)
        or draft.validation_sha256 != _sha(validation)
        or validation.plan_sha256 != _sha(plan)
    ):
        fail("FORMAL_FREEZE_MISMATCH", "冻结草案引用的候选、计划或验证快照已变更。")
    if candidate.purpose == "formal":
        if isinstance(draft, FormalFreezeDraftV2):
            fail("FORMAL_EXECUTION_UNAVAILABLE", "正式执行与隔离尚未开放，不能冻结或解锁。")
        fail("FORMAL_VALIDATION_SOURCE_UNVERIFIED", "当前验证快照缺少已核验的生产响应来源。")
    if not isinstance(draft, FreezeDraft):
        fail("FORMAL_FREEZE_MISMATCH", "合成夹具须继续使用旧版冻结草案。")
    selected = next(
        (
            row
            for row in validation.reports
            if row.experiment_id == validation.selected_experiment_id
        ),
        None,
    )
    if selected is None:
        fail("FORMAL_FREEZE_MISMATCH", "验证快照缺少所选实验。")
    if (
        draft.code_digest != selected.code_digest
        or draft.scorer_version != selected.scorer_version
        or draft.metrics_version != selected.metrics_version
        or draft.model_snapshot_digest != selected.model_snapshot_digest
        or draft.policies_digest != plan.policies_digest
        or draft.retrieval_digest != plan.retrieval_digest
        or draft.budget_usd != 0
    ):
        fail("FORMAL_FREEZE_MISMATCH", "冻结版本、配置摘要或预算与验证快照不一致。")
    return {
        "id": selected.experiment_id,
        "mode": selected.mode,
        "strategy": selected.strategy,
        "config_sha256": selected.config_sha256,
    }


def _validate_formal_draft(candidate, plan, validation, draft, *, execution_cap=None):
    if (
        candidate.purpose != "formal"
        or draft.candidate_sha256 != _sha(candidate)
        or draft.manifest_sha256 != candidate.manifest_sha256
        or draft.validation_sha256 != _sha(validation)
        or draft.validation_plan_sha256 != _sha(plan)
        or validation.plan_sha256 != draft.validation_plan_sha256
        or draft.case_count != sum(candidate.distribution.values())
    ):
        fail("FORMAL_FREEZE_MISMATCH", "草案与候选、清单、验证计划或快照不一致。")
    if (
        not isinstance(validation, NativeValidationSnapshot)
        or validation.source_verification != "NATIVE_VALIDATION_VERIFIED"
        or not validation.controls_by_condition
    ):
        fail("FORMAL_VALIDATION_SOURCE_UNVERIFIED", "缺少带完整运行控制的已核验原生快照。")
    controls = validation.controls_by_condition
    if (
        set(draft.conditions) != plan.config_sha256.keys()
        or controls.keys() != draft.conditions.keys()
        or str(draft.selected_validation_experiment_id) != validation.selected_experiment_id
    ):
        fail("FORMAL_FREEZE_MISMATCH", "正式条件矩阵或选择与原生验证不一致。")
    for key, condition in draft.conditions.items():
        locked = controls[key]
        source_id = str(locked.validation_experiment_id)
        observed = validation.evidence.get(source_id)
        if (
            condition.validation_experiment_id != locked.validation_experiment_id
            or condition.controls_sha256 != locked.controls_sha256
            or locked.controls_sha256 != _sha(locked.controls)
            or locked.config_sha256 != plan.config_sha256[key]
            or f"{locked.controls.mode}/{locked.controls.strategy}" != key
            or locked.controls.retrieval_sha256 != plan.retrieval_digest
            or observed is None
            or observed.source_blockers != []
            or not any(
                report.experiment_id == source_id
                and f"{report.mode}/{report.strategy}" == key
                and report.source == locked.controls.source
                and report.config_sha256 == locked.config_sha256
                and report.code_digest == locked.controls.code_digest
                and report.scorer_version == locked.controls.scorer_version
                and report.metrics_version == locked.controls.metrics_version
                and report.model_snapshot_digest == locked.controls.model_snapshot_sha256
                for report in validation.reports
            )
        ):
            fail("FORMAL_FREEZE_MISMATCH", "条件控制与锁存验证配置不一致。")
        if (
            source_id == validation.selected_experiment_id
            and locked.gate_policy is not None
            and draft.gate_policy != locked.gate_policy
        ):
            fail("FORMAL_FREEZE_MISMATCH", "正式门槛须保留所选验证实验已有的门槛正文。")
    if execution_cap is not None and (
        draft.campaign_quota.calls > execution_cap.calls
        or draft.campaign_quota.budget_microusd > execution_cap.budget_microusd
    ):
        fail("FORMAL_BUDGET_CAP_EXCEEDED", "草案超过当前正式执行封顶。")


def _case_fingerprints(cases):
    keys = set()
    if not isinstance(cases, list) or not 1 <= len(cases) <= 144:
        return keys
    for case in cases:
        if not isinstance(case, dict):
            return set()
        message, ident = case.get("message"), case.get("id")
        if not isinstance(message, str) or not isinstance(ident, str) or not ident:
            return set()
        expression = "".join(unicodedata.normalize("NFKC", message).casefold().split())
        if not expression:
            return set()
        keys.add(digest(["expression-v1", expression]))
        # Record the input independently of the public case ID and script event labels.
        events = case.get("events")
        if isinstance(events, list) and all(isinstance(event, dict) for event in events):
            steps = [
                {key: value for key, value in event.items() if key != "id"} for event in events
            ]
            keys.add(digest(["case-v1", expression, case.get("fixture"), steps]))
    return keys


def _inspect_package(candidate, path, on_cases):
    location = Path(path)
    if location.is_relative_to(ROOT):
        fail("FORMAL_PATH_INVALID", "留出包应保存在开发工作区之外。")
    with directory(location) as fd:
        raw = read(fd, "manifest.json")
        if _hash(raw) != candidate.manifest_sha256:
            fail("FORMAL_PACKAGE_MISMATCH", "留出清单与登记摘要不一致。")
        manifest = PackageManifest.model_validate(json_data(raw))
        cases_raw = read(fd, "cases.json", maximum=MAX_PACKAGE)
        cases = json_data(cases_raw)
        on_cases(_case_fingerprints(cases))
        refs_raw = read(fd, "references.json", maximum=MAX_PACKAGE)
    if (
        manifest.dataset_id != candidate.dataset_id
        or _hash(cases_raw) != manifest.cases_sha256
        or _hash(refs_raw) != manifest.references_sha256
        or _old_inputs() & {manifest.cases_sha256, manifest.references_sha256}
    ):
        fail("FORMAL_PACKAGE_MISMATCH", "留出数据摘要不一致，或引用了已暴露的文件。")
    references = json_data(refs_raw)
    if not isinstance(cases, list) or not isinstance(references, dict):
        fail("FORMAL_PACKAGE_INVALID", "留出案例和参考格式无效。")
    if len(cases) != sum(candidate.distribution.values()) or len(references) != len(cases):
        fail("FORMAL_PACKAGE_INVALID", "留出案例、参考或登记数量不一致。")
    intents, identifiers = Counter(), set()
    for case in cases:
        if not isinstance(case, dict):
            fail("FORMAL_PACKAGE_INVALID", "留出案例格式无效。")
        ident = case.get("id")
        events = case.get("events")
        if (
            not isinstance(ident, str)
            or not 1 <= len(ident) <= 64
            or ident.upper().startswith("AC02-")
            or ident in identifiers
            or case.get("split") != "holdout"
            or not isinstance(case.get("message"), str)
            or not case["message"].strip()
            or not isinstance(case.get("fixture"), dict)
            or not isinstance(events, list)
        ):
            fail("FORMAL_PACKAGE_INVALID", "留出案例标识、划分或输入无效。")
        expected = references.get(ident)
        created = [
            event.get("args")
            for event in events
            if isinstance(event, dict) and event.get("op") == "create_case"
        ]
        if (
            not isinstance(expected, dict)
            or not isinstance(expected.get("family"), str)
            or not expected["family"].strip()
            or len(created) != 1
            or not isinstance(created[0], dict)
            or expected.get("intent") not in candidate.distribution
            or created[0].get("intent") != expected["intent"]
        ):
            fail("FORMAL_PACKAGE_INVALID", "留出参考、场景和脚本意图不一致。")
        identifiers.add(ident)
        intents[expected["intent"]] += 1
    if dict(intents) != candidate.distribution:
        fail("FORMAL_PACKAGE_INVALID", "留出意图分布与登记不一致。")
    return manifest


def _other_records(root, campaign_id, reader):
    for name in os.listdir(root):
        if name == campaign_id:
            continue
        try:
            TypeAdapter(Identifier).validate_python(name)
        except ValidationError:
            continue
        other = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            _private(other)
            value = reader(other)
        finally:
            os.close(other)
        yield value


def _status(candidate, frozen, events):
    kinds = {event.kind for event in events}
    if candidate.exposed_before_freeze or "EXPOSED_BEFORE_FREEZE" in kinds:
        return "EXPOSED_BEFORE_FREEZE"
    if any(isinstance(event, _BoundExposureEvent) for event in events):
        attempt = _package_reads(events)[-1]
        if (
            frozen is None
            or attempt.opening.freeze_sha256 != _hash(json_bytes(frozen))
            or attempt.terminal is None
            or attempt.terminal.kind == "EXPOSURE_UNCERTAIN"
        ):
            return "EXPOSURE_UNCERTAIN"
        return "UNLOCKED"
    if "EXPOSURE_UNCERTAIN" in kinds:
        return "EXPOSURE_UNCERTAIN"
    if "UNLOCKED" in kinds:
        return "UNLOCKED"
    if "OPENING" in kinds:
        return "EXPOSURE_UNCERTAIN"
    return "FROZEN" if frozen is not None else "REGISTERED"


def _prior_exposure(root, campaign_id):
    prior = set()
    for events in _other_records(root, campaign_id, _events):
        if any(isinstance(event, _BoundExposureEvent) for event in events):
            for attempt in _package_reads(events):
                if attempt.cases is None or (
                    not attempt.cases.case_fingerprints
                    and attempt.cases.matching_case_fingerprints_sha256 is None
                ):
                    fail(
                        "FORMAL_OTHER_EXPOSURE_UNCERTAIN",
                        "此账本已有无法核对案例范围的解锁记录。",
                    )
        fingerprints = {key for event in events for key in event.case_fingerprints or []}
        if not fingerprints and any(event.kind == "OPENING" for event in events):
            fail("FORMAL_OTHER_EXPOSURE_UNCERTAIN", "此账本已有无法核对案例范围的解锁记录。")
        prior.update(fingerprints)
    return prior


def _formal_selection(validation):
    report = next(
        row for row in validation.reports if row.experiment_id == validation.selected_experiment_id
    )
    return {
        "id": validation.selected_experiment_id,
        "mode": report.mode,
        "strategy": report.strategy,
        "config_sha256": report.config_sha256,
    }


def _original_unlock(events, freeze_sha256, manifest_sha256):
    if not events or not all(isinstance(event, _BoundExposureEvent) for event in events):
        return None
    initial = _package_reads(events)[0]
    terminal = initial.terminal
    if (
        terminal is not None
        and terminal.kind == "UNLOCKED"
        and terminal.freeze_sha256 == freeze_sha256
        and terminal.package_binding.manifest_sha256 == manifest_sha256
    ):
        return initial
    return None


def _verify_readback(fd, event):
    recorded = _events(fd)
    _journal_require(
        bool(recorded) and recorded[-1] == event and _package_reads(recorded)[-1].terminal == event
    )


def _read_bound_package(fd, opening, reader, *, prior=(), original=None):
    def record_cases(cases):
        try:
            fingerprints = sorted(_case_fingerprints(cases))
        except (ValueError, TypeError, RecursionError):
            fingerprints = []
        if original is not None and fingerprints == original:
            payload = {"matching_case_fingerprints_sha256": digest(fingerprints)}
        else:
            payload = {"case_fingerprints": fingerprints}
        _bound_event(fd, "CASES_READ", opening, "已登记本次读取的案例范围。", **payload)
        if not fingerprints:
            fail("FORMAL_PACKAGE_INVALID", "无法核对本次读取的案例范围。")
        if set(fingerprints).intersection(prior):
            fail("FORMAL_DATASET_EXPOSED", "此账本中另一登记曾打开相同案例内容。")
        if original is not None and fingerprints != original:
            fail("FORMAL_PACKAGE_CHANGED", "重开的案例范围与成功解锁记录不一致。")

    try:
        view = reader(record_cases)
        if opening.kind == "OPENING":
            if _old_inputs() & {view.binding.cases_sha256, view.binding.references_sha256}:
                fail("FORMAL_DATASET_EXPOSED", "候选包引用了已暴露的文件。")
            event = _bound_event(
                fd,
                "UNLOCKED",
                opening,
                "已核对私有候选包并绑定原冻结。",
                package_binding=view.binding,
            )
        else:
            event = _bound_event(fd, "REOPENED", opening, "已按原冻结和成功绑定重新核对候选包。")
        _verify_readback(fd, event)
    except (BusinessError, OSError, ValueError, TypeError, RecursionError):
        try:
            _bound_event(fd, "EXPOSURE_UNCERTAIN", opening, "本次读取或核验失败，保留暴露不确定。")
        except (BusinessError, OSError):
            # A durable opening without a valid terminal event still blocks later reads.
            pass
        raise BusinessError(
            "FORMAL_PACKAGE_OPEN_FAILED", "留出包打开或核验失败。该候选保留暴露不确定状态。"
        ) from None
    return view


class CampaignRegister:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)

    @contextmanager
    def _campaign(self, campaign_id, *, writing=False, create=False):
        ident = TypeAdapter(Identifier).validate_python(campaign_id)
        with directory(self.root, create=create) as root:
            _private(root)
            fcntl.flock(root, fcntl.LOCK_EX if writing else fcntl.LOCK_SH)
            try:
                if create:
                    try:
                        os.mkdir(ident, mode=0o700, dir_fd=root)
                        os.fsync(root)
                    except FileExistsError:
                        pass
                fd = os.open(ident, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                try:
                    _private(fd)
                    yield root, fd
                finally:
                    os.close(fd)
            finally:
                fcntl.flock(root, fcntl.LOCK_UN)

    def register(self, campaign_id, candidate: Candidate):
        if candidate.dataset_id in {"v0.1", "v0.2"} or candidate.manifest_sha256 in _old_inputs():
            fail("FORMAL_DATASET_EXPOSED", "已公开的历史数据不能登记为正式未见候选。")
        with self._campaign(campaign_id, writing=True, create=True) as (root, fd):
            for record in _other_records(
                root, campaign_id, lambda other: _optional(other, "candidate.json")
            ):
                if record and (
                    record["dataset_id"] == candidate.dataset_id
                    or record["manifest_sha256"] == candidate.manifest_sha256
                ):
                    fail("FORMAL_CANDIDATE_ALREADY_REGISTERED", "候选版本或摘要已经登记。")
            write_once(fd, "candidate.json", candidate.model_dump(mode="json"))
        return {
            "campaign_id": campaign_id,
            "candidate_sha256": _sha(candidate),
            "status": _status(candidate, None, []),
            "purpose": candidate.purpose,
            "human_identity": HUMAN_IDENTITY,
        }

    def plan(self, campaign_id, plan: ValidationPlan):
        with self._campaign(campaign_id, writing=True) as (_, fd):
            _candidate(fd)
            write_once(fd, "validation-plan.json", plan.model_dump(mode="json"))
        return {"campaign_id": campaign_id, "plan_sha256": _sha(plan)}

    def validation(self, campaign_id, validation: Validation):
        with self._campaign(campaign_id, writing=True) as (_, fd):
            if _optional(fd, "validation.json") is not None:
                fail("FORMAL_RECORD_EXISTS", "原验证快照不可覆写。")
            snapshot = _reports(_candidate(fd), self._plan(fd), validation)
            write_once(fd, "validation.json", snapshot.model_dump(mode="json"))
        return {
            "campaign_id": campaign_id,
            "validation_sha256": _sha(snapshot),
            "source_verification": snapshot.source_verification,
            "human_identity": HUMAN_IDENTITY,
        }

    def record_exposure(self, campaign_id, human: HumanRecord):
        with self._campaign(campaign_id, writing=True) as (_, fd):
            _candidate(fd)
            _event(fd, "EXPOSED_BEFORE_FREEZE", human, "登记选型前明文接触。")
        return {
            "campaign_id": campaign_id,
            "status": "EXPOSED_BEFORE_FREEZE",
            "human_identity": HUMAN_IDENTITY,
        }

    def validation_native(self, campaign_id, request: NativeValidation, reader):
        with self._campaign(campaign_id, writing=True) as (_, fd):
            if _candidate(fd).purpose != "formal":
                fail("FORMAL_NATIVE_PURPOSE_REQUIRED", "原生验证快照仅登记到正式候选。")
            if _optional(fd, "validation-native.json") is not None:
                fail("FORMAL_RECORD_EXISTS", "原生验证快照不可覆写。")
            plan = self._plan(fd)
            if request.plan_sha256 != _sha(plan):
                fail("FORMAL_VALIDATION_MISMATCH", "请求未绑定原验证计划。")
            snapshot = reader(plan, request)
            write_once(fd, "validation-native.json", snapshot.model_dump(mode="json"))
        return {
            "campaign_id": campaign_id,
            "native_validation_sha256": _sha(snapshot),
            "source_verification": snapshot.source_verification,
            "evidence": {
                key: value.model_dump(mode="json") for key, value in snapshot.evidence.items()
            },
            "controls_by_condition": {
                key: value.model_dump(mode="json")
                for key, value in (snapshot.controls_by_condition or {}).items()
            },
            "human_identity": HUMAN_IDENTITY,
        }

    @staticmethod
    def _plan(fd):
        value = _optional(fd, "validation-plan.json")
        if value is None:
            fail("FORMAL_VALIDATION_PLAN_REQUIRED", "请先登记验证集输入全集和对照配置。")
        return ValidationPlan.model_validate(value)

    @staticmethod
    def _validation(fd):
        native = _optional(fd, "validation-native.json")
        if native is not None:
            return NativeValidationSnapshot.model_validate(native)
        value = _optional(fd, "validation.json")
        if value is None:
            fail("FORMAL_VALIDATION_REQUIRED", "请先登记验证实验和选型。")
        return ValidationSnapshot.model_validate(value)

    @staticmethod
    def _checked_freeze(fd, candidate, campaign_id, *, execution_cap=None):
        frozen = _optional(fd, "freeze.json")
        if frozen is None:
            return None
        if (
            not isinstance(frozen, dict)
            or set(frozen)
            != {
                "schema_version",
                "campaign_id",
                "draft",
                "selected",
                "confirmation",
                "human_identity",
            }
            or frozen["schema_version"] != "ac-formal-freeze-v2"
            or frozen["campaign_id"] != campaign_id
            or frozen["human_identity"] != HUMAN_IDENTITY
        ):
            fail("FORMAL_FREEZE_MISMATCH", "冻结记录格式或登记标识不一致。")
        draft = parse_freeze_draft(frozen["draft"])
        confirmation = FreezeConfirmation.model_validate(frozen["confirmation"])
        if confirmation.draft_sha256 != _sha(draft):
            fail("FORMAL_FREEZE_MISMATCH", "冻结草案与人工确认摘要不同。")
        plan = CampaignRegister._plan(fd)
        validation = CampaignRegister._validation(fd)
        if confirmation.human.at < max(candidate.custodian.at, validation.selection.at):
            fail("FORMAL_CONFIRMATION_MISMATCH", "冻结确认早于候选登记或验证选型。")
        if isinstance(draft, FormalFreezeDraftV2):
            _validate_formal_draft(candidate, plan, validation, draft, execution_cap=execution_cap)
            selected = _formal_selection(validation)
        else:
            selected = _validate_draft(candidate, plan, validation, draft)
        if selected != frozen["selected"]:
            fail("FORMAL_FREEZE_MISMATCH", "冻结记录和原选型配置不一致。")
        return frozen, draft, confirmation, validation

    @staticmethod
    def _frozen(fd, candidate, campaign_id):
        checked = CampaignRegister._checked_freeze(fd, candidate, campaign_id)
        return checked[0] if checked is not None else None

    def _freeze_payload(self, fd, campaign_id, candidate, draft, confirmation, *, execution_cap=None):
        if _sha(draft) != confirmation.draft_sha256:
            fail("FORMAL_CONFIRMATION_MISMATCH", "人工确认没有绑定本次冻结草案。")
        if _optional(fd, "freeze.json") is not None:
            fail("FORMAL_RECORD_EXISTS", "原冻结记录不可覆写。")
        if _status(candidate, None, _events(fd)) != "REGISTERED":
            fail("FORMAL_DATASET_EXPOSED", "候选已有暴露记录，不能冻结为未见数据。")
        plan = self._plan(fd)
        validation = self._validation(fd)
        if confirmation.human.at < max(candidate.custodian.at, validation.selection.at):
            fail("FORMAL_CONFIRMATION_MISMATCH", "冻结确认早于候选登记或验证选型。")
        if isinstance(draft, FormalFreezeDraftV2):
            _validate_formal_draft(candidate, plan, validation, draft, execution_cap=execution_cap)
            selected = _formal_selection(validation)
        else:
            selected = _validate_draft(candidate, plan, validation, draft)
        return {
            "schema_version": "ac-formal-freeze-v2",
            "campaign_id": campaign_id,
            "draft": draft.model_dump(mode="json"),
            "selected": selected,
            "confirmation": confirmation.model_dump(mode="json"),
            "human_identity": HUMAN_IDENTITY,
        }

    def preview_freeze(self, campaign_id, draft: FormalFreezeDraftV2, confirmation: FreezeConfirmation):
        """Derive the exact freeze digest from local metadata before any deployment approval."""
        if not isinstance(draft, FormalFreezeDraftV2):
            fail("FORMAL_FREEZE_MISMATCH", "正式冻结预览须使用新版草案。")
        with self._campaign(campaign_id) as (_, fd):
            payload = self._freeze_payload(fd, campaign_id, _candidate(fd), draft, confirmation)
        return {
            "campaign_id": campaign_id,
            "status": "DRAFT_VALIDATED_ONLY",
            "draft_sha256": confirmation.draft_sha256,
            "prospective_freeze_sha256": _hash(json_bytes(payload)),
            "execution_cap_status": "UNCONFIGURED",
            "freeze_available": False,
            "execution_available": False,
            "deployment_authorization": "NOT_CHECKED",
            "human_identity": HUMAN_IDENTITY,
        }

    def freeze(
        self,
        campaign_id,
        draft: FreezeDraft | FormalFreezeDraftV2,
        confirmation: FreezeConfirmation,
        *,
        execution_cap: FreezeQuota | None = None,
        expected_freeze_sha256: str | None = None,
    ):
        formal = isinstance(draft, FormalFreezeDraftV2)
        if formal and (not isinstance(execution_cap, FreezeQuota) or expected_freeze_sha256 is None):
            fail("FORMAL_EXECUTION_UNAVAILABLE", "正式冻结需要绑定预览摘要的部署额度。")
        with self._campaign(campaign_id, writing=True) as (_, fd):
            candidate = _candidate(fd)
            frozen = self._freeze_payload(
                fd, campaign_id, candidate, draft, confirmation, execution_cap=execution_cap
            )
            freeze_sha256 = _hash(json_bytes(frozen))
            if formal and freeze_sha256 != expected_freeze_sha256:
                fail("FORMAL_FREEZE_MISMATCH", "当前冻结记录与已授权的预览摘要不同。")
            write_once(fd, "freeze.json", frozen)
        return {
            "campaign_id": campaign_id,
            "status": "FROZEN",
            "freeze_sha256": freeze_sha256,
            "purpose": candidate.purpose,
            "human_identity": HUMAN_IDENTITY,
        }

    def preflight(self, campaign_id):
        with self._campaign(campaign_id) as (_, fd):
            candidate = _candidate(fd)
            checked = self._checked_freeze(fd, candidate, campaign_id)
            frozen = checked[0] if checked is not None else None
            events = _events(fd)
            raw_native = _optional(fd, "validation-native.json")
            raw_validation = _optional(fd, "validation.json")
            validation = (
                NativeValidationSnapshot.model_validate(raw_native)
                if raw_native is not None
                else ValidationSnapshot.model_validate(raw_validation)
                if raw_validation is not None
                else None
            )
            raw_plan = _optional(fd, "validation-plan.json")
            plan = ValidationPlan.model_validate(raw_plan) if raw_plan is not None else None
            if validation is not None and (plan is None or validation.plan_sha256 != _sha(plan)):
                fail("FORMAL_VALIDATION_MISMATCH", "本地验证快照与原计划不一致。")
            status = _status(candidate, frozen, events)
            freeze_sha256 = _hash(json_bytes(frozen)) if frozen is not None else None
            execution_available = bool(
                checked is not None
                and isinstance(checked[1], FormalFreezeDraftV2)
                and status == "UNLOCKED"
                and _original_unlock(events, freeze_sha256, candidate.manifest_sha256) is not None
            )
        return {
            "campaign_id": campaign_id,
            "status": status,
            "purpose": candidate.purpose,
            "dataset_id": candidate.dataset_id,
            "distribution": candidate.distribution,
            "candidate_sha256": _sha(candidate),
            "manifest_sha256": candidate.manifest_sha256,
            "plan_sha256": _sha(plan) if plan else None,
            "validation_sha256": _sha(validation) if validation else None,
            "source_verification": validation.source_verification if validation else None,
            "native_validation_sha256": _sha(validation) if raw_native is not None else None,
            "human_identity": HUMAN_IDENTITY,
            "exposure_scope": "THIS_LOCAL_REGISTER_ONLY",
            "freeze_sha256": freeze_sha256,
            "exposure_events": len(events),
            "execution_available": execution_available,
            "deployment_authorization": "NOT_CHECKED",
        }

    def check_draft(
        self,
        campaign_id,
        draft: FreezeDraft | FormalFreezeDraftV2,
        *,
        execution_cap: FreezeQuota | None = None,
    ):
        with self._campaign(campaign_id) as (_, fd):
            candidate = _candidate(fd)
            if _status(candidate, _optional(fd, "freeze.json"), _events(fd)) != "REGISTERED":
                fail("FORMAL_FREEZE_BLOCKED", "候选当前不可生成新的冻结确认。")
            if isinstance(draft, FormalFreezeDraftV2):
                _validate_formal_draft(
                    candidate,
                    self._plan(fd),
                    self._validation(fd),
                    draft,
                    execution_cap=execution_cap,
                )
                return {
                    "campaign_id": campaign_id,
                    "draft_sha256": _sha(draft),
                    "status": "DRAFT_VALIDATED_ONLY",
                    "execution_cap_status": "UNCONFIGURED"
                    if execution_cap is None
                    else "WITHIN_CAP",
                    "freeze_available": False,
                    "execution_available": False,
                    "human_identity": HUMAN_IDENTITY,
                }
            selected = _validate_draft(candidate, self._plan(fd), self._validation(fd), draft)
        return {
            "campaign_id": campaign_id,
            "draft_sha256": _sha(draft),
            "selected": selected,
            "human_identity": HUMAN_IDENTITY,
        }

    def unlock(
        self, campaign_id, request: Unlock, *,
        execution_cap: FreezeQuota | None = None,
        expected_freeze_sha256: str | None = None,
    ):
        if execution_cap is not None or expected_freeze_sha256 is not None:
            if expected_freeze_sha256 != request.freeze_sha256:
                fail("FORMAL_FREEZE_MISMATCH", "解锁交接与部署批准的冻结摘要不同。")
            return self._prepare_unlock(campaign_id, request, execution_cap=execution_cap)
        with self._campaign(campaign_id, writing=True) as (root, fd):
            candidate = _candidate(fd)
            if candidate.purpose == "formal":
                fail("FORMAL_UNLOCK_BLOCKED", "正式解锁需要绑定本次冻结的部署额度。")
            frozen = self._frozen(fd, candidate, campaign_id)
            if _status(candidate, frozen, _events(fd)) != "FROZEN":
                fail("FORMAL_UNLOCK_BLOCKED", "候选尚未冻结，或已有打开、暴露或不确定记录。")
            if _hash(json_bytes(frozen)) != request.freeze_sha256:
                fail("FORMAL_FREEZE_MISMATCH", "解锁交接的冻结摘要不一致。")
            confirmed_at = FreezeConfirmation.model_validate(frozen["confirmation"]).human.at
            if request.handoff.at < confirmed_at:
                fail("FORMAL_UNLOCK_BLOCKED", "解锁交接早于冻结确认。")
            previously_read = _prior_exposure(root, campaign_id)
            _event(
                fd,
                "OPENING",
                request.handoff,
                "已记录交接，准备读取留出目录。",
                freeze_sha256=request.freeze_sha256,
            )

            def record_cases(fingerprints):
                if not fingerprints:
                    fail("FORMAL_PACKAGE_INVALID", "无法登记已读取的案例范围。")
                _event(
                    fd,
                    "CASES_READ",
                    request.handoff,
                    "已读取案例并登记逐案内容摘要。",
                    freeze_sha256=request.freeze_sha256,
                    case_fingerprints=sorted(fingerprints),
                )
                if previously_read & fingerprints:
                    fail("FORMAL_DATASET_EXPOSED", "此账本中另一登记曾打开相同案例内容。")

            try:
                package = _inspect_package(candidate, request.directory, record_cases)
            except (BusinessError, OSError, ValueError, TypeError, RecursionError):
                _event(
                    fd,
                    "EXPOSURE_UNCERTAIN",
                    request.handoff,
                    "打开或核验失败，保留暴露记录。",
                    freeze_sha256=request.freeze_sha256,
                )
                fail(
                    "FORMAL_PACKAGE_OPEN_FAILED", "留出包打开或核验失败。该候选保留暴露不确定状态。"
                )
            _event(
                fd,
                "UNLOCKED",
                request.handoff,
                "已读取明文并核对登记清单。",
                freeze_sha256=request.freeze_sha256,
                package=package,
            )
        return {
            "campaign_id": campaign_id,
            "status": "UNLOCKED",
            "purpose": candidate.purpose,
            "human_identity": HUMAN_IDENTITY,
            "exposure_scope": "THIS_LOCAL_REGISTER_ONLY",
            "freeze_sha256": request.freeze_sha256,
            "execution_available": False,
        }

    def _prepare_unlock(self, campaign_id, request: Unlock, *, execution_cap: FreezeQuota):
        """Open and bind a formal candidate after the caller verifies its deployment."""
        if not isinstance(execution_cap, FreezeQuota):
            fail("FORMAL_BUDGET_CAP_REQUIRED", "执行前须登记当前费用与尝试次数上限。")
        with self._campaign(campaign_id, writing=True) as (root, fd):
            candidate = _candidate(fd)
            checked = self._checked_freeze(fd, candidate, campaign_id, execution_cap=execution_cap)
            events = _events(fd)
            if (
                checked is None
                or not isinstance(checked[1], FormalFreezeDraftV2)
                or _status(candidate, checked[0], events) != "FROZEN"
            ):
                fail("FORMAL_UNLOCK_BLOCKED", "候选尚未冻结，或已有打开、暴露或不确定记录。")
            frozen, _, confirmation, _ = checked
            if _hash(json_bytes(frozen)) != request.freeze_sha256:
                fail("FORMAL_FREEZE_MISMATCH", "解锁交接的冻结摘要不一致。")
            if request.handoff.at < confirmation.human.at:
                fail("FORMAL_UNLOCK_BLOCKED", "解锁交接早于冻结确认。")
            prior = _prior_exposure(root, campaign_id)
            opening = _begin_package_read(fd, "OPENING", request.handoff, request.freeze_sha256)
            _read_bound_package(
                fd,
                opening,
                lambda record: load_private_package(
                    request.directory,
                    manifest_sha256=candidate.manifest_sha256,
                    dataset_id=candidate.dataset_id,
                    distribution=candidate.distribution,
                    on_cases=record,
                ),
                prior=prior,
            )
        return {
            "campaign_id": campaign_id,
            "status": "UNLOCKED",
            "purpose": candidate.purpose,
            "human_identity": HUMAN_IDENTITY,
            "exposure_scope": "THIS_LOCAL_REGISTER_ONLY",
            "freeze_sha256": request.freeze_sha256,
            "execution_available": True,
        }

    def open_for_execution(
        self,
        campaign_id,
        *,
        expected_freeze_sha256: str,
        execution_cap: FreezeQuota,
        operator: HumanRecord,
    ) -> ExecutionHandoff:
        return self._prepare_execution(
            campaign_id, expected_freeze_sha256=expected_freeze_sha256,
            execution_cap=execution_cap, operator=operator,
        )

    def _prepare_execution(
        self,
        campaign_id,
        *,
        expected_freeze_sha256: str,
        execution_cap: FreezeQuota,
        operator: HumanRecord,
    ) -> ExecutionHandoff:
        """Revalidate the ledger and reopen the package under the caller's batch lock."""
        if not isinstance(execution_cap, FreezeQuota):
            fail("FORMAL_BUDGET_CAP_REQUIRED", "执行前须登记当前费用与尝试次数上限。")
        with self._campaign(campaign_id, writing=True) as (_, fd):
            candidate = _candidate(fd)
            checked = self._checked_freeze(fd, candidate, campaign_id, execution_cap=execution_cap)
            events = _events(fd)
            if (
                checked is None
                or not isinstance(checked[1], FormalFreezeDraftV2)
                or _status(candidate, checked[0], events) != "UNLOCKED"
                or not all(isinstance(event, _BoundExposureEvent) for event in events)
            ):
                fail("FORMAL_EXECUTION_BLOCKED", "候选没有可用于执行的成功解锁绑定。")
            frozen, draft, _, validation = checked
            freeze_sha256 = _hash(json_bytes(frozen))
            if freeze_sha256 != expected_freeze_sha256:
                fail("FORMAL_FREEZE_MISMATCH", "执行请求的冻结摘要已变化。")
            initial = _original_unlock(events, freeze_sha256, draft.manifest_sha256)
            if initial is None:
                fail("FORMAL_FREEZE_MISMATCH", "私有候选绑定与原冻结或登记不一致。")
            binding = initial.terminal.package_binding
            if operator.at < initial.opening.human.at:
                fail("FORMAL_EXECUTION_BLOCKED", "本次操作者记录早于原解锁交接。")
            opening = _begin_package_read(fd, "REOPENING", operator, freeze_sha256)
            view = _read_bound_package(
                fd,
                opening,
                lambda record: reopen_private_package(
                    binding,
                    dataset_id=candidate.dataset_id,
                    distribution=candidate.distribution,
                    on_cases=record,
                ),
                original=initial.cases.case_fingerprints,
            )
            return ExecutionHandoff(
                freeze_sha256=freeze_sha256,
                package=view,
                _draft=json_bytes(draft.model_dump(mode="json")),
                _controls=json_bytes(
                    {
                        key: control.model_dump(mode="json")
                        for key, control in validation.controls_by_condition.items()
                    }
                ),
            )
