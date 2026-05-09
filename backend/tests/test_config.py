"""Tests for backend/config.py — Settings model validation and defaults."""

import pytest
from pathlib import Path
from unittest.mock import patch


def test_default_settings_load():
    """Settings should load with default values without errors."""
    from backend.config import settings

    assert settings.llm_base_url.startswith("http")
    assert settings.default_model
    assert settings.skill_resource_max_chars > 0
    assert settings.skill_command_timeout > 0
    assert settings.llm_timeout_seconds > 0


def test_new_fields_declared():
    """All fields that were previously accessed via getattr should now be proper attrs."""
    from backend.config import Settings

    fields = Settings.model_fields
    expected = [
        "llm_base_url",
        "default_model",
        "openai_api_key",
        "llm_api_key",
        "planner_model",
        "temperature",
        "max_tokens",
        "llm_timeout_seconds",
        "kernel_path",
        "skills_path",
        "skill_resource_max_chars",
        "skill_command_timeout",
    ]
    for name in expected:
        assert name in fields, f"Missing field: {name}"


def test_planner_model_defaults_to_none():
    from backend.config import settings

    assert settings.planner_model is None or isinstance(settings.planner_model, str)


def test_temperature_defaults_to_none():
    from backend.config import settings

    assert settings.temperature is None


def test_max_tokens_defaults_to_none():
    from backend.config import settings

    assert settings.max_tokens is None


def test_skills_path_created_if_missing(tmp_path):
    """skills_path should be created automatically when it doesn't exist."""
    from backend.config import Settings

    kernel = tmp_path / "kernel"
    kernel.mkdir()
    # A non-existent skills path should be auto-created
    new_skills = tmp_path / "skills_new"
    assert not new_skills.exists()
    s = Settings(kernel_path=kernel, skills_path=new_skills)
    assert new_skills.is_dir()


def test_kernel_path_must_exist(tmp_path):
    """kernel_path validator should raise when the directory doesn't exist."""
    from backend.config import Settings

    missing_kernel = tmp_path / "no_kernel_here"
    with pytest.raises(Exception):
        Settings(kernel_path=missing_kernel, skills_path=tmp_path / "skills")
