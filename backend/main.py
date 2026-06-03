import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
)
logging.getLogger().setLevel(logging.INFO)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(logging.Formatter(_LOG_FORMAT))

from .routers import chat, creator, creator_chat, health, sandbox_chat, skills, skills_chat

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
