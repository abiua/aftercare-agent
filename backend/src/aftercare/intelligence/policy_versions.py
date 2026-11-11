"""Deterministic, line-scoped policy version selection shared by rules and search."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from aftercare.business.rules import digest

TIME_BASES = {
    "decision_at",
    "case_created_at",
    "order_created_at",
    "paid_at",
    "delivered_at",
}


class PolicyReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str


class PolicyVersion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)
    status: str = "PUBLISHED"
    source: str = Field(default="synthetic:aftercare-v0.1", min_length=1)
    intents: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    time_basis: str = "decision_at"
    priority: StrictInt = 0
    effective_at: datetime
    expires_at: datetime | None = None
    published_at: datetime | None = None
    withdrawn_at: datetime | None = None
    supersedes: list[PolicyReference] = Field(default_factory=list)


def _stamp(value) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Timestamp requires an offset")
    return stamp


@dataclass(frozen=True)
class PolicyResolution:
    documents: list[dict]
    by_product: dict[str, list[dict]]
    blockers: list[str]
    snapshot: str

    def to_dict(self):
        return {
            "documents": self.documents,
            "by_product": self.by_product,
            "blockers": self.blockers,
            "snapshot": self.snapshot,
        }


def _valid_version(row: PolicyVersion) -> bool:
    if row.status not in {"DRAFT", "PUBLISHED", "WITHDRAWN"} or row.time_basis not in TIME_BASES:
        return False
    if row.status == "WITHDRAWN" and row.withdrawn_at is None:
        return False
    if row.status == "PUBLISHED" and row.withdrawn_at is not None:
        return False
    dates = [row.effective_at, row.expires_at, row.published_at, row.withdrawn_at]
    if any(value is not None and value.utcoffset() is None for value in dates):
        return False
    return (
        (row.expires_at is None or row.effective_at < row.expires_at)
        and (
            row.withdrawn_at is None
            or row.published_at is None
            or row.published_at < row.withdrawn_at
        )
        and all(ref.id == row.id and ref.version != row.version for ref in row.supersedes)
    )


def _availability(row, *, intent, product, timestamps, blockers):
    if row.status == "DRAFT" or (intent is not None and row.intents and intent not in row.intents):
        return False, False
    product_id = product["id"]
    if row.product_ids and product_id not in row.product_ids:
        return False, False
    category = product.get("category")
    if row.categories and category is not None and category not in row.categories:
        return False, False
    try:
        at = _stamp(timestamps.get(row.time_basis))
    except (ValueError, TypeError):
        blockers.add(f"UNKNOWN_POLICY_TIME:{row.id}:{product_id}:{row.time_basis}")
        return False, False
    started = row.effective_at <= at and (row.published_at is None or row.published_at <= at)
    active = (
        started
        and (row.expires_at is None or at < row.expires_at)
        and (row.withdrawn_at is None or at < row.withdrawn_at)
    )
    if row.categories and category is None and (active or (started and row.supersedes)):
        blockers.add(f"UNKNOWN_POLICY_PRODUCT_SCOPE:{row.id}:{product_id}")
        return False, False
    return active, started and bool(row.supersedes)


def _ancestors(key, indexed, visiting):
    if key in visiting:
        raise ValueError("SUPERSESSION_CYCLE")
    if key not in indexed:
        raise ValueError("SUPERSESSION_TARGET_MISSING")
    result = set()
    for ref in indexed[key].supersedes:
        parent = (ref.id, ref.version)
        result.add(parent)
        result.update(_ancestors(parent, indexed, visiting | {key}))
    return result


def resolve_policies(
    rows: list[dict],
    *,
    intent: str | None,
    products: list[dict],
    timestamps: dict[str, datetime | str | None],
) -> PolicyResolution:
    blockers: set[str] = set()
    indexed: dict[tuple[str, str], PolicyVersion] = {}
    originals: dict[tuple[str, str], dict] = {}
    for raw in rows:
        label = raw.get("id", "?")
        try:
            row = PolicyVersion.model_validate(raw)
        except ValidationError:
            blockers.add(f"INVALID_POLICY_VERSION:{label}")
            continue
        if not _valid_version(row):
            blockers.add(f"INVALID_POLICY_VERSION:{row.id}:{row.version}")
            continue
        key = row.id, row.version
        if key in originals and digest(originals[key]) != digest(raw):
            blockers.add(f"CONFLICTING_POLICY_VERSION_CONTENT:{row.id}:{row.version}")
            continue
        indexed[key] = row
        originals[key] = raw

    by_product = {}
    for product in sorted(products or [{"id": "*"}], key=lambda item: item["id"]):
        candidates = {}
        superseded = set()
        for key, row in indexed.items():
            active, replaces = _availability(
                row,
                intent=intent,
                product=product,
                timestamps=timestamps,
                blockers=blockers,
            )
            if replaces:
                try:
                    superseded.update(_ancestors(key, indexed, set()))
                except ValueError as error:
                    blockers.add(f"POLICY_{error}:{row.id}:{row.version}:{product['id']}")
                    continue
            if active:
                candidates[key] = row
        grouped: dict[str, list[PolicyVersion]] = {}
        for key, row in candidates.items():
            if key not in superseded:
                grouped.setdefault(row.id, []).append(row)
        selected = []
        for policy_id, group in sorted(grouped.items()):

            def rank(row):
                specificity = 2 * bool(row.product_ids) + bool(row.categories)
                return row.priority, specificity, row.effective_at

            best = max(map(rank, group))
            winners = [row for row in group if rank(row) == best]
            if len(winners) != 1:
                blockers.add(f"CONFLICTING_POLICY_PRIORITY:{policy_id}:{product['id']}")
            else:
                selected.append(originals[(winners[0].id, winners[0].version)])
        by_product[product["id"]] = selected

    unique = {
        (policy["id"], policy["version"]): policy
        for policies in by_product.values()
        for policy in policies
    }
    documents = [unique[key] for key in sorted(unique)]
    reasons = sorted(blockers)
    snapshot = digest({"by_product": by_product, "blockers": reasons})
    return PolicyResolution(documents, by_product, reasons, snapshot)
