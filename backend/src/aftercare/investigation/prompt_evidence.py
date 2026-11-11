"""Versioned model projection for scoped investigation evidence."""

from aftercare.business.rules import digest
from aftercare.intelligence.models import ModelFailure

PROMPT_VERSION = "compact-v5"
PRIVATE_RECEIPT_EVENT = "investigation_receipt"
PROMPT_LEGEND = (
    "证据结构：scope 依次为会话、事项、工单。"
    "ref 数组依次为来源类型、ID、版本或摘要、可选观察时间；所有 ref 继承该 scope。"
    "available_evidence 仅用于选择工具读取。客户陈述、附件和观察即使出现在输入中，也不会自动成为可引用证据。"
    "supporting_refs、conflicting_refs、evidence_refs、evidence_ids 只能逐字使用本轮 citable_evidence_ids 中的 ID。"
    "在这些证据字段中，条款使用完整版本键，例如 POL06:1.0。缺少可引用证据时保留未解决项。"
    "depends_on_claims 只能填写本次输出其他主张的 id 或 upstream_claim_ids 中的 id。"
    "不要在主张依赖中填写证据 ID，也不要依赖自身或形成循环。"
    "original_read_receipts 的 ref 对应 original_frozen_evidence 的原始数据。"
    "工具 data.retrieval 中的 proof_sha256 对应系统另存的完整检索诊断。"
)


def _failure():
    raise ModelFailure("INVESTIGATION_EVIDENCE_INCONSISTENT")


class PromptEvidence:
    def __init__(self, source):
        self.scope = [source.conversation_id, source.issue_id, source.case_id]
        self.input_digest = source.relevant_input_sha256

    def ref(self, value):
        if value["scope"] != self.scope:
            _failure()
        result = [value["source_kind"], value["source_id"], value["version_or_sha256"]]
        if value.get("observed_at") is not None:
            result.append(value["observed_at"])
        return result

    def record(self, value):
        return {**value, "ref": self.ref(value["ref"])}

    def receipt(self, value, *, separate_records=False):
        if value["input_digest"] != self.input_digest:
            _failure()
        data = dict(value["data"])
        refs = [self.ref(ref) for ref in value["refs"]]
        had_records = "records" in data
        records = [self.record(row) for row in data.pop("records", [])]
        record_refs = [row["ref"] for row in records]
        if had_records and record_refs != refs:
            _failure()
        if "retrieval" in data:
            retrieval = data["retrieval"]
            data["retrieval"] = {
                **{
                    key: retrieval[key]
                    for key in (
                        "mode",
                        "candidate_scope",
                        "applicability",
                        "corpus_sha256",
                        "hybrid_fallback",
                    )
                    if key in retrieval
                },
                "proof_sha256": digest(retrieval),
            }
        if had_records and not separate_records:
            data["records"] = records
        result = {
            "name": value["name"],
            "arguments_sha256": value["arguments_sha256"],
            "data": data,
        }
        if separate_records or not had_records:
            result["refs"] = refs
        return result, records

    def prompt(self, view):
        view = dict(view)
        if "available_evidence" in view:
            view["available_evidence"] = [self.ref(ref) for ref in view["available_evidence"]]
        for key in ("customer_statements", "attachments", "unverified_observations"):
            if key in view:
                view[key] = [self.record(row) for row in view[key]]
        if "check_receipt" in view:
            view["check_receipt"], _ = self.receipt(view["check_receipt"])
        if "original_read_receipts" in view:
            originals = {}
            receipts = []
            for row in view.get("original_frozen_evidence", []):
                self._add(originals, self.record(row))
            for receipt in view["original_read_receipts"]:
                projected, records = self.receipt(receipt, separate_records=True)
                for row in records:
                    self._add(originals, row)
                receipts.append(projected)
            view["original_read_receipts"] = receipts
            view["original_frozen_evidence"] = list(originals.values())
        return {**view, "scope": self.scope, "evidence_encoding": PROMPT_VERSION}

    @staticmethod
    def _add(index, row):
        key = tuple(row["ref"][:3])
        existing = index.setdefault(key, row)
        if existing != row:
            _failure()
