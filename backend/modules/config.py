import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / ".env")

KERNEL_PATH: str = os.getenv("KERNEL_PATH", "kernel")
SKILL_DATA_PATH: str = os.getenv("SKILL_DATA_PATH", "skill-data")
LLM_HOST: str = os.getenv("LLM_HOST", "localhost")
LLM_PORT: int = int(os.getenv("LLM_PORT", "11434"))
LLM_MODEL: str = os.getenv("LLM_MODEL", "llama3")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8000"))

# Always resolve paths relative to project root
KERNEL_PATH = str(_root / KERNEL_PATH)
SKILL_DATA_PATH = str(_root / SKILL_DATA_PATH)
