"""FastAPI application entry point."""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routers.skill import router

app = FastAPI(title="Skill Creator Platform", version="1.0.0")

# Configure CORS from environment; defaults to localhost only.
# Set CORS_ORIGINS=* in .env only for local development.
_cors_origins_env = os.getenv("CORS_ORIGINS", "http://localhost,http://localhost:80")
_cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok"}
