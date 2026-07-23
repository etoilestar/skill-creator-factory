import asyncio
from unittest.mock import AsyncMock, patch


def test_profile_resolution_falls_back_per_field(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    monkeypatch.setattr(settings, "governance_path", tmp_path)
    monkeypatch.setattr(settings, "llm_base_url", "http://global.test")
    monkeypatch.setattr(settings, "max_tokens", 321)
    monkeypatch.setattr(profiles, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "global-planner"})())
    profiles._save({"planner": {"base_url": "https://planner.test/v1", "api_key": "secret-A", "model": "planner-A", "max_tokens": None}, "reviewer": {"base_url": "", "api_key": "", "model": "", "max_tokens": None}})
    resolved = profiles.resolve_creator_model_profile("planner")
    assert (resolved.base_url, resolved.api_key, resolved.model, resolved.max_tokens) == ("https://planner.test/v1", "secret-A", "planner-A", 321)


def test_role_calls_do_not_leak_provider_overrides(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    monkeypatch.setattr(settings, "governance_path", tmp_path)
    monkeypatch.setattr(profiles, "route_model", lambda task, **kwargs: type("Route", (), {"model": f"global-{task}"})())
    profiles._save({"planner": {"base_url": "https://a.test", "api_key": "key-A", "model": "model-A", "max_tokens": 1234}, "reviewer": {"base_url": "https://b.test", "api_key": "key-B", "model": "model-B", "max_tokens": 2345}})
    with patch.object(profiles, "complete_chat_once", new=AsyncMock(return_value="ok")) as call:
        asyncio.run(profiles.complete_creator_role_once([], "planner"))
        asyncio.run(profiles.complete_creator_role_once([], "reviewer"))
    assert call.await_args_list[0].kwargs == {"base_url": "https://a.test", "api_key": "key-A", "max_tokens": 1234}
    assert call.await_args_list[0].args[1] == "model-A"
    assert call.await_args_list[1].kwargs == {"base_url": "https://b.test", "api_key": "key-B", "max_tokens": 2345}


def test_public_profile_never_contains_api_key():
    from backend.services.creator_model_profiles import _public
    result = _public("planner", {"base_url": "https://example.test", "api_key": "top-secret", "model": "model", "max_tokens": 1})
    assert "top-secret" not in str(result)
    assert result["api_key_configured"] is True
