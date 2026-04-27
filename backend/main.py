from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import chat, health, skills

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
app.include_router(skills.router)
