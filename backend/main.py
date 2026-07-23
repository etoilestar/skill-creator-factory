import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())

    formatter = logging.Formatter(_LOG_FORMAT)
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)

    log_dir = Path(os.environ.get("LOG_DIR") or "/app/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "backend.log"

    resolved_log_path = str(log_path.resolve())
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", None) == resolved_log_path:
            return

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=50 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


_configure_logging()

from .routers import chat, creator, creator_chat, creator_tools, health, sandbox_chat, skills, skills_chat, publish, publish_gateway
from .services.creator_model_profiles import router as creator_model_profiles_router


app = FastAPI(title="Skill Creator Factory", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(creator_chat.router)
app.include_router(sandbox_chat.router)
app.include_router(skills_chat.router)
app.include_router(skills.router)
app.include_router(creator.router)
app.include_router(creator_model_profiles_router)

app.include_router(publish.router)
app.include_router(publish_gateway.router)

app.include_router(creator_tools.router)
