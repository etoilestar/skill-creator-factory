from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    llm_base_url: str = "http://localhost:11434"
    default_model: str = "qwen3:32b"#"llama3.2"
    # Leave empty to use a local backend; set to your sk-... key for OpenAI.
    openai_api_key: Optional[str] = None
    kernel_path: Path = PROJECT_ROOT / "kernel"
    skills_path: Path = PROJECT_ROOT / "skills"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
