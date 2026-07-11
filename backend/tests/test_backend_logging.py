import importlib
import logging
from logging.handlers import RotatingFileHandler


def test_configure_logging_keeps_console_adds_file_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    import backend.main as main

    main = importlib.reload(main)
    root = logging.getLogger()

    assert any(not isinstance(handler, RotatingFileHandler) for handler in root.handlers)
    file_handlers = [handler for handler in root.handlers if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str((tmp_path / "backend.log").resolve())]
    assert len(file_handlers) == 1

    logging.getLogger("backend.services.creator").info("unit-test-backend-log-line")
    for handler in root.handlers:
        handler.flush()
    assert "unit-test-backend-log-line" in (tmp_path / "backend.log").read_text(encoding="utf-8")

    main._configure_logging()
    file_handlers_after = [handler for handler in root.handlers if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str((tmp_path / "backend.log").resolve())]
    assert len(file_handlers_after) == 1
