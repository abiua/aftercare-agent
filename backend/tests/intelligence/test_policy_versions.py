from aftercare.intelligence.policies import PolicySearch, document_key
from aftercare.intelligence.policy_versions import resolve_policies

BASE = {
    "id": "POL06",
    "version": "1",
    "title": "损坏补证",
    "text": "需要商品和包裹照片。",
    "effective_at": "2026-01-01T00:00:00+08:00",
}
PRODUCTS = [{"id": "P1", "category": "数码"}, {"id": "P2", "category": "家具"}]
CLOCK = {
    "decision_at": "2026-05-01T00:00:00+08:00",
    "case_created_at": "2026-02-01T00:00:00+08:00",
}


def resolve(*policies, timestamps=CLOCK):
    return resolve_policies(
        list(policies), intent="damaged", products=PRODUCTS, timestamps=timestamps
    )


def test_history_uses_configured_basis_and_scope_per_product():
    old = {**BASE, "time_basis": "case_created_at"}
    new = {**old, "version": "2", "effective_at": "2026-03-01T00:00:00+08:00"}
    scoped = {**old, "version": "sku", "product_ids": ["P1"], "categories": ["数码"]}
    unpublished = {**old, "version": "draft", "status": "DRAFT", "priority": 100}
    result = resolve(new, scoped, unpublished, old)
    assert not result.blockers
    assert result.by_product == {"P1": [scoped], "P2": [old]}
    assert result.snapshot == resolve(old, unpublished, scoped, new).snapshot
    assert set(map(document_key, result.documents)) == {document_key(scoped), document_key(old)}
    search = PolicySearch(None).search_selected("包裹照片", result.documents)
    assert {ref["version"] for ref in search.lexical_versions} == {"1", "sku"}


def test_priority_conflict_is_never_silently_resolved():
    conflicting = {**BASE, "version": "2", "text": "不需要照片。"}
    result = resolve(BASE, conflicting)
    assert not result.documents
    assert result.blockers == [
        "CONFLICTING_POLICY_PRIORITY:POL06:P1",
        "CONFLICTING_POLICY_PRIORITY:POL06:P2",
    ]
    high = {**conflicting, "priority": 4, "categories": ["数码"]}
    resolved = resolve(BASE, high)
    assert resolved.by_product == {"P1": [high], "P2": [BASE]}


def test_withdrawn_expired_and_not_yet_published_policies_follow_timeline():
    withdrawn = {
        **BASE,
        "status": "WITHDRAWN",
        "withdrawn_at": "2026-04-01T00:00:00+08:00",
    }
    assert resolve(withdrawn).documents == []
    assert resolve(withdrawn, timestamps={"decision_at": "2026-03-01T00:00:00+08:00"}).documents
    scheduled = {**BASE, "published_at": "2026-07-01T00:00:00+08:00"}
    assert resolve(scheduled).documents == []
    expiring = {**BASE, "expires_at": CLOCK["decision_at"]}
    assert resolve(expiring).documents == []


def test_explicit_supersession_does_not_revive_retired_predecessor():
    future = {
        **BASE,
        "version": "2",
        "effective_at": "2026-03-01T00:00:00+08:00",
        "expires_at": "2026-04-01T00:00:00+08:00",
        "supersedes": [{"id": "POL06", "version": "1"}],
    }
    assert resolve(BASE, future).documents == []
    assert resolve(
        BASE, future, timestamps={"decision_at": CLOCK["case_created_at"]}
    ).documents == [BASE]
    prior = {**BASE, "supersedes": [{"id": "POL06", "version": "2"}]}
    assert any("CYCLE" in reason for reason in resolve(prior, future).blockers)


def test_invalid_version_unknown_timestamp_and_product_scope_are_explicit():
    unknown_clock = {**BASE, "time_basis": "delivered_at"}
    assert all("UNKNOWN_POLICY_TIME" in item for item in resolve(unknown_clock).blockers)
    scoped = {**BASE, "categories": ["数码"]}
    unknown = resolve_policies(
        [scoped], intent="damaged", products=[{"id": "P1"}], timestamps=CLOCK
    )
    assert unknown.blockers == ["UNKNOWN_POLICY_PRODUCT_SCOPE:POL06:P1"]
    changed_same_version = {**BASE, "text": "另一条冲突内容。"}
    assert (
        "CONFLICTING_POLICY_VERSION_CONTENT:POL06:1" in resolve(BASE, changed_same_version).blockers
    )
    missing_ancestor = {**BASE, "supersedes": [{"id": "POL06", "version": "missing"}]}
    assert all("TARGET_MISSING" in reason for reason in resolve(missing_ancestor).blockers)
