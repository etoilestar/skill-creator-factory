import logging
import subprocess

import pytest

from backend.routers import chat_utils


def test_dependency_install_logs_start_completion_and_duration(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(
        chat_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )

    with caplog.at_level(logging.INFO, logger=chat_utils.logger.name):
        details = chat_utils._install_python_import_dependency("sample_import", tmp_path / "python")

    assert details["package"] == "sample_import"
    assert "dependency install started" in caplog.text
    assert "dependency install completed" in caplog.text
    assert "sample_import" in caplog.text
    assert "duration_ms=" in caplog.text


def test_dependency_install_failure_logs_and_propagates(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(
        chat_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr="failed"),
    )

    with caplog.at_level(logging.WARNING, logger=chat_utils.logger.name), pytest.raises(RuntimeError):
        chat_utils._install_python_import_dependency("sample_import", tmp_path / "python")

    assert "dependency install failed" in caplog.text
    assert "sample_import" in caplog.text
    assert "duration_ms=" in caplog.text


def test_dependency_install_timeout_logs_and_propagates(monkeypatch, caplog, tmp_path):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="pip install", timeout=180)

    monkeypatch.setattr(chat_utils.subprocess, "run", timeout)

    with caplog.at_level(logging.WARNING, logger=chat_utils.logger.name), pytest.raises(subprocess.TimeoutExpired):
        chat_utils._install_python_import_dependency("sample_import", tmp_path / "python")

    assert "dependency install timed out" in caplog.text
    assert "sample_import" in caplog.text
    assert "duration_ms=" in caplog.text
