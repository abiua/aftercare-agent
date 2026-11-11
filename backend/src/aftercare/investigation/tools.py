"""Read-only tools over a scoped frozen snapshot."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError

from aftercare.business.refund_lookup import locate_refund
from aftercare.business.rules import digest
from aftercare.intelligence.policies import PolicySearch, bm25, document_key

from .contracts import ORDER_OWNERSHIP_REF, Contract, EvidenceRef, InvestigationInput, Role
from .material_observations import ORDER_PAYMENT_REF

Id = Annotated[str, Field(min_length=1, max_length=160)]
Ids = Annotated[list[Id], Field(min_length=1, max_length=20)]
OptionalIds = Annotated[list[Id], Field(max_length=20)]


class LineArgs(Contract):
    line_ids: Ids


class MaterialArgs(Contract):
    evidence_ids: Ids


class RefundArgs(Contract):
    execution_id: Id


class PolicyArgs(Contract):
    query: Annotated[str, Field(min_length=1, max_length=300)]
    condition_keys: OptionalIds = Field(default_factory=list)


class ClauseArgs(Contract):
    policy_id: Id
    version: Id
    product_id: Id


class TargetArgs(Contract):
    claim_id: Id
    conflicting_refs: OptionalIds
    target_tool: Literal[
        "read_order_lines",
        "read_material_evidence",
        "read_refund_execution",
        "search_applicable_policy",
        "read_policy_clause",
    ]
    reason: Annotated[str, Field(min_length=1, max_length=400)]


class ToolReceipt(Contract):
    name: str
    arguments_sha256: str
    input_digest: str
    refs: tuple[EvidenceRef, ...] = ()
    data: dict


class ToolRejected(ValueError):
    pass


_SCHEMAS = {
    "read_order_lines": LineArgs,
    "read_material_evidence": MaterialArgs,
    "read_refund_execution": RefundArgs,
    "search_applicable_policy": PolicyArgs,
    "read_policy_clause": ClauseArgs,
    "request_targeted_check": TargetArgs,
}
_FACT = ("read_order_lines", "read_material_evidence", "read_refund_execution")
_POLICY = ("search_applicable_policy", "read_policy_clause")
_ALLOWED = {
    "strong_single": (*_FACT, *_POLICY),
    "fact": _FACT,
    "policy": _POLICY,
    "verifier": ("request_targeted_check",),
}
_MATERIAL = ("ATTACHMENT", "OBSERVATION", "MATERIAL", "REVIEW")


class InvestigationTools:
    def __init__(self, value: InvestigationInput, *, policy_search: PolicySearch | None = None):
        self._input = value.model_copy(deep=True)
        self._rows = {(row.ref.source_kind, row.ref.source_id): row for row in self._input.evidence}
        self._search = policy_search

    def schemas(self, role: Role) -> list[dict]:
        descriptions = (
            {
                "search_applicable_policy": "Rank raw frozen policy candidates, including historical and inapplicable versions. No applicability judgment.",
                "read_policy_clause": "Read an exact raw policy version. product_id must be in the frozen case; the clause may not apply to it.",
            }
            if self._input.policy_catalogue_version
            else {}
        )
        descriptions.update(
            read_order_lines="Read order lines and any frozen authenticated account ownership record.",
            read_material_evidence="Read material records and their effective review, if any. Material review does not establish payment approval or image content.",
        )
        if ("CONDITION", ORDER_PAYMENT_REF) in self._rows:
            descriptions["read_order_lines"] = (
                "Read order lines, frozen order payment status and timestamp, "
                "and any authenticated account ownership record."
            )
            descriptions["read_material_evidence"] = (
                "Read material records, their effective review, and any recorded structured "
                "human observations. Observations do not establish image authenticity, cause, "
                "eligibility, or approval."
            )
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": _SCHEMAS[name].model_json_schema(),
                    "description": descriptions.get(name, name.replace("_", " ")),
                },
            }
            for name in _ALLOWED.get(role, ())
        ]

    def validate_call(self, role: Role, name: str, args: dict):
        if name not in _ALLOWED.get(role, ()):
            raise ToolRejected("TOOL_NOT_ALLOWED")
        try:
            args = _SCHEMAS[name].model_validate(args)
        except ValidationError:
            raise ToolRejected("INVALID_TOOL_ARGUMENTS") from None
        self._selected(name, args)
        return args

    def _select(self, kinds, ids):
        if len(set(ids)) != len(ids):
            raise ToolRejected("DUPLICATE_EVIDENCE_REFERENCE")
        selected = []
        for ident in ids:
            matches = [self._rows[(kind, ident)] for kind in kinds if (kind, ident) in self._rows]
            if len(matches) != 1:
                raise ToolRejected("EVIDENCE_NOT_AVAILABLE")
            selected.extend(matches)
        return selected

    def _selected(self, name, args):
        if name == "read_order_lines":
            return self._select(("ORDER_LINE",), args.line_ids)
        if name == "read_material_evidence":
            return self._select(_MATERIAL, args.evidence_ids)
        if name == "read_refund_execution":
            return self._select(("EXECUTION",), [args.execution_id])
        if name == "search_applicable_policy":
            return self._select(("CONDITION",), args.condition_keys)
        if name == "read_policy_clause":
            rows = self._select(("POLICY",), [f"{args.policy_id}:{args.version}"])
            products = (
                {row["id"] for row in self._input.policy_context["products"]}
                if self._input.policy_catalogue_version
                else set(rows[0].data["selected_for"])
            )
            if args.product_id not in products:
                raise ToolRejected("EVIDENCE_NOT_AVAILABLE")
            return rows
        return self._select(tuple({kind for kind, _ in self._rows}), args.conflicting_refs)

    def call(self, role: Role, name: str, args: dict) -> ToolReceipt:
        validated = self.validate_call(role, name, args)
        rows = self._selected(name, validated)
        if name == "read_order_lines":
            for ident in (ORDER_OWNERSHIP_REF, ORDER_PAYMENT_REF):
                if (record := self._rows.get(("CONDITION", ident))) is not None:
                    rows.append(record)
        if name == "read_material_evidence":
            materials = {row.ref.source_id for row in rows if row.ref.source_kind == "MATERIAL"}
            explicit = set(validated.evidence_ids)
            rows.extend(
                row
                for (kind, ident), row in self._rows.items()
                if kind == "REVIEW"
                and ident not in explicit
                and materials.intersection(row.data.get("material_ids", []))
            )
        if name == "search_applicable_policy":
            rows, data = self._search_policy(validated.query, rows)
        elif name == "read_refund_execution":
            data = locate_refund([row.data for row in rows], validated.execution_id)
        elif name == "request_targeted_check":
            data = {"status": "REQUESTED", **validated.model_dump(mode="json")}
        else:
            data = {"records": [row.model_dump(mode="json") for row in rows]}
            if name == "read_policy_clause" and self._input.policy_catalogue_version:
                data["applicability"] = "NOT_ASSESSED"
        return ToolReceipt(
            name=name,
            arguments_sha256=digest(validated.model_dump(mode="json")),
            input_digest=self._input.relevant_input_sha256,
            refs=tuple(row.ref for row in rows),
            data=data,
        )

    def _search_policy(self, query, conditions):
        policies = {
            document_key(row.data["clause"]): row
            for (kind, _), row in self._rows.items()
            if kind == "POLICY"
        }
        docs = {key: row.data["clause"] for key, row in policies.items()}
        if self._search is None:
            ranking = bm25(
                query,
                {
                    key: " ".join(str(doc.get(field, "")) for field in ("title", "text"))
                    for key, doc in docs.items()
                },
            )[:5]
            audit = {"mode": "BM25", "lexical": ranking}
        else:
            result = self._search.search_selected(
                query,
                list(docs.values()),
                blockers=()
                if self._input.policy_catalogue_version
                else self._input.policy_blockers,
            )
            ranking = [document_key(doc) for doc in result.documents]
            audit = {"mode": result.mode, "retrieval_evidence": result.evidence}
        if self._input.policy_catalogue_version:
            audit.update(
                corpus_sha256=self._input.policy_context["catalogue_sha256"],
                candidate_scope="FROZEN_RAW_CATALOGUE",
                applicability="NOT_ASSESSED",
            )
            if (
                self._search is not None
                and getattr(self._search, "encode", None) is not None
                and audit["mode"] == "BM25"
            ):
                audit["hybrid_fallback"] = "HYBRID_NOT_APPLIED_TO_COMPLETE_FROZEN_CORPUS"
        rows = [*conditions, *(policies[key] for key in ranking)]
        return rows, {"records": [row.model_dump(mode="json") for row in rows], "retrieval": audit}
