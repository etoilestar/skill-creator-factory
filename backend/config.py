from pathlib import Path
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    llm_base_url: str = "http://localhost:11434"
    default_model: str = "llama3.2"
    kernel_path: Path = PROJECT_ROOT / "kernel"
    skills_path: Path = PROJECT_ROOT / "skills"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
