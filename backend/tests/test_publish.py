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


class TestPublishAuth:
    """Tests for publish_auth.py service."""

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

    def test_get_active_published_models(self):
        from backend.services.publish_auth import get_active_published_models
        from backend.services.publish_config import save_publish_config

        save_publish_config({
            "endpoint_id": "",
            "name": "active-model",
            "enabled_skills": [],
            "is_active": True,
        })
        save_publish_config({
            "endpoint_id": "",
            "name": "inactive-model",
            "enabled_skills": [],
            "is_active": False,
        })

        active = get_active_published_models()
        names = [c["name"] for c in active]
        assert "active-model" in names
        assert "inactive-model" not in names
