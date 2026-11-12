def test_investigation_run_budget_preserves_defaults_and_registry_caps(monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    import pytest
    from aftercare.config import Settings
    from aftercare.intelligence import service
    from aftercare.intelligence.models import ModelRunner, Registry
    from pydantic import ValidationError

    settings_fields = {
        "database_url": "fixture",
        "object_endpoint": "fixture",
        "object_access_key": "fixture",
        "object_secret_key": "fixture",
        "redis_url": "fixture",
    }
    default = Settings(**settings_fields)
    expanded = Settings(
        **settings_fields,
        chat_investigation_run_max_calls=12,
        chat_investigation_direct_recheck_request=True,
    )
    assert default.chat_investigation_run_max_calls == 8
    assert default.chat_investigation_direct_recheck_request is False
    with pytest.raises(ValidationError):
        Settings(**settings_fields, chat_investigation_run_max_calls=13)

    value = SimpleNamespace(policy_catalogue_version=service.CATALOGUE_VERSION)
    monkeypatch.setattr(service, "build_input", lambda scope: value)
    monkeypatch.setattr(service, "route_input", lambda value: SimpleNamespace(path="MULTI"))
    monkeypatch.setattr(service, "InvestigationTools", lambda *args, **kwargs: None)
    scope = SimpleNamespace(line_ids=(), input_refs=(), frozen_context={})
    model = ModelRunner(
        Registry(
            version="fixture",
            models=[],
            max_attempts=12,
            max_run_attempts=10,
            max_usd=Decimal("0.70"),
            max_run_usd=Decimal("0.50"),
        )
    )
    legacy = SimpleNamespace(
        model=model, search=None, audit=None, validate_draft=None, strategy="routed"
    )
    for settings, call_cap, run_cap in (
        (default, 8, 8),
        (SimpleNamespace(), 8, 8),
        (expanded, 12, 10),
    ):
        agent = service.AgentService(None, settings, None, None, model=model, search=object())
        context = agent._investigation_context(legacy, scope, "auto")
        assert context.direct_recheck_request is (settings is expanded)
        effective = context.model.registry
        assert effective.max_attempts == call_cap
        assert effective.max_run_attempts == run_cap
        assert effective.max_run_usd == Decimal("0.50")
