"""Tests for the publish module services and routes."""

import json
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def publish_tmp_dir(tmp_path, monkeypatch):
    """Use a temp directory for publish configs during tests."""
    publish_path = tmp_path / "publish"
    publish_path.mkdir()
    monkeypatch.setattr(
        "backend.services.publish_config.settings.publish_config_path", publish_path
    )
    return publish_path


class TestPublishConfig:
    """Tests for publish_config.py service."""

    def test_load_empty_configs(self):
        from backend.services.publish_config import load_publish_configs

        configs = load_publish_configs()
        assert configs == []

    def test_save_and_load_config(self):
        from backend.services.publish_config import (
            load_publish_configs,
            save_publish_config,
        )

        config = save_publish_config({
            "endpoint_id": "",
            "name": "test-model",
            "enabled_skills": ["skill-a"],
            "is_active": True,
        })

        assert config["name"] == "test-model"
        assert config["endpoint_id"]  # generated
        assert config["api_key"].startswith("sk-pub-")
        assert config["enabled_skills"] == ["skill-a"]

        loaded = load_publish_configs()
        assert len(loaded) == 1
        assert loaded[0]["name"] == "test-model"

    def test_get_config(self):
        from backend.services.publish_config import (
            get_publish_config,
            save_publish_config,
        )

        config = save_publish_config({
            "endpoint_id": "",
            "name": "my-model",
            "enabled_skills": [],
            "is_active": False,
        })

        found = get_publish_config(config["endpoint_id"])
        assert found is not None
        assert found["name"] == "my-model"

        not_found = get_publish_config("nonexistent")
        assert not_found is None

    def test_delete_config(self):
        from backend.services.publish_config import (
            delete_publish_config,
            load_publish_configs,
            save_publish_config,
        )

        config = save_publish_config({
            "endpoint_id": "",
            "name": "to-delete",
            "enabled_skills": [],
            "is_active": False,
        })

        assert delete_publish_config(config["endpoint_id"]) is True
        assert load_publish_configs() == []
        assert delete_publish_config("nonexistent") is False

    def test_toggle_config(self):
        from backend.services.publish_config import (
            save_publish_config,
            toggle_publish_config,
        )

        config = save_publish_config({
            "endpoint_id": "",
            "name": "toggle-me",
            "enabled_skills": [],
            "is_active": False,
        })

        toggled = toggle_publish_config(config["endpoint_id"])
        assert toggled["is_active"] is True

        toggled = toggle_publish_config(config["endpoint_id"])
        assert toggled["is_active"] is False

    def test_regenerate_key(self):
        from backend.services.publish_config import (
            regenerate_api_key,
            save_publish_config,
        )

        config = save_publish_config({
            "endpoint_id": "",
            "name": "regen-key",
            "enabled_skills": [],
            "is_active": False,
        })
        old_key = config["api_key"]

        updated = regenerate_api_key(config["endpoint_id"])
        assert updated["api_key"] != old_key
        assert updated["api_key"].startswith("sk-pub-")

    def test_get_config_by_model_name(self):
        from backend.services.publish_config import (
            get_config_by_model_name,
            save_publish_config,
        )

        save_publish_config({
            "endpoint_id": "",
            "name": "unique-model",
            "enabled_skills": [],
            "is_active": True,
        })

        found = get_config_by_model_name("unique-model")
        assert found is not None
        assert found["name"] == "unique-model"

        not_found = get_config_by_model_name("nonexistent")
        assert not_found is None

    def test_get_config_by_api_key(self):
        from backend.services.publish_config import (
            get_config_by_api_key,
            save_publish_config,
        )

        config = save_publish_config({
            "endpoint_id": "",
            "name": "key-lookup",
            "enabled_skills": [],
            "is_active": True,
        })

        found = get_config_by_api_key(config["api_key"])
        assert found is not None
        assert found["endpoint_id"] == config["endpoint_id"]


class TestPublishAuth:
    """Tests for publish_auth.py service."""

    def test_verify_valid_token(self):
        from backend.services.publish_auth import verify_publish_token
        from backend.services.publish_config import save_publish_config

        config = save_publish_config({
            "endpoint_id": "",
            "name": "auth-test",
            "enabled_skills": [],
            "is_active": True,
        })

        result = verify_publish_token(config["api_key"])
        assert result is not None
        assert result["endpoint_id"] == config["endpoint_id"]

    def test_verify_invalid_token(self):
        from backend.services.publish_auth import verify_publish_token

        assert verify_publish_token("invalid-key") is None
        assert verify_publish_token("") is None
        assert verify_publish_token(None) is None

    def test_verify_inactive_config(self):
        from backend.services.publish_auth import verify_publish_token
        from backend.services.publish_config import save_publish_config

        config = save_publish_config({
            "endpoint_id": "",
            "name": "inactive-test",
            "enabled_skills": [],
            "is_active": False,
        })

        result = verify_publish_token(config["api_key"])
        assert result is None

    def test_rate_limit(self):
        from backend.services.publish_auth import check_rate_limit, _request_log

        _request_log.clear()

        # With default limit of 60, first 60 should pass
        endpoint_id = "test-endpoint"
        for _ in range(60):
            assert check_rate_limit(endpoint_id) is True

        # 61st should fail
        assert check_rate_limit(endpoint_id) is False

        _request_log.clear()
