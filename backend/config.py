from pathlib import Path
from typing import Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    # LLM backend
    llm_base_url: str = "http://localhost:11434"
    default_model: str = "qwen3:32b"
    # Leave empty to use a local backend; set to your sk-... key for OpenAI.
    openai_api_key: Optional[str] = None
    # Alternative field name accepted by some deployments
    llm_api_key: Optional[str] = None

    # Optional separate model for silent planner rounds (metadata / block / skill planner).
    # Falls back to default_model when unset.
    planner_model: Optional[str] = None

    # LLM generation parameters — omitted from request payload when unset so the
    # backend can apply its own defaults.
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    # Temperature override for silent JSON-decision rounds (metadata / child-skill /
    # resource-selection / runtime-planner / block-planner).  Low values (0.0–0.1)
    # reduce the chance the model adds prose or Markdown to a pure-JSON response.
    # Falls back to 0.0 when unset, giving deterministic planner behaviour.
    planner_temperature: Optional[float] = None

    # Timeout for LLM HTTP requests in seconds.
    llm_timeout_seconds: int = 6000

    # Filesystem paths
    kernel_path: Path = PROJECT_ROOT / "kernel"
    skills_path: Path = PROJECT_ROOT / "skills"
    managed_skills_path: Path = PROJECT_ROOT / "skills"
    workspace_skills_path: Path = PROJECT_ROOT / ".agents" / "skills"
    shared_skills_path: Path = Path.home() / ".agents" / "skills"
    bundled_skills_path: Path = PROJECT_ROOT / "bundled-skills"
    governance_path: Path = PROJECT_ROOT / ".skill-governance"

    # Resource reading limit per file (characters) used by read_skill_resource_text.
    skill_resource_max_chars: int = 20000

    # Maximum wall-clock seconds allowed for a single run_command subprocess.
    skill_command_timeout: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @model_validator(mode="after")
    def _ensure_paths(self) -> "Settings":
        """Create skills_path if it does not exist; kernel_path must already exist."""
        self.managed_skills_path = self.skills_path
        self.managed_skills_path.mkdir(parents=True, exist_ok=True)
        self.workspace_skills_path.mkdir(parents=True, exist_ok=True)
        self.shared_skills_path.mkdir(parents=True, exist_ok=True)
        self.bundled_skills_path.mkdir(parents=True, exist_ok=True)
        self.governance_path.mkdir(parents=True, exist_ok=True)
        self.skills_path = self.managed_skills_path
        if not self.kernel_path.exists():
            raise ValueError(
                f"kernel_path does not exist: {self.kernel_path}. "
                "Ensure the kernel/ directory is present in the project root."
            )
        return self


settings = Settings()
