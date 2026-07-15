from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Contest Dataset Backend MVP"
    app_env: Literal["development", "test", "production"] = "development"
    storage_root: Path = Path("storage")
    export_root: Path | None = None
    testlib_root: Path = Path("/opt/testlib")
    jngen_root: Path = Path("/opt/jngen")
    storage_volume: str = "contest_dataset_storage"
    runner_compile_image: str = "contest-dataset-runner-compiler:0.3.0"
    runner_execute_image: str = "contest-dataset-runner-executor:0.3.0"
    runner_memory: str = "512m"
    runner_nano_cpus: int = 1_000_000_000
    runner_timeout_seconds: int = 30
    runner_concurrency: int = Field(default=2, ge=1, le=8)
    runner_batch_size: int = Field(default=16, ge=1, le=64)
    manifest_checkpoint_interval: int = Field(default=10, ge=1, le=100)
    max_tool_calls_per_run: int = 8
    max_log_chars: int = 16_000

    model_base_url: str = "https://api.deepseek.com/v1"
    model_api_key: str = ""
    model_name: str = "deepseek-chat"
    model_timeout_seconds: float = 120.0
    agent_max_iterations: int = Field(default=4, ge=1, le=12)
    # Agent4 first performs a bounded document-retrieval conversation.  Keeping
    # this separate from code-repair iterations prevents a failed compile from
    # consuming the document-selection budget.
    agent_jngen_document_selection_rounds: int = Field(default=3, ge=2, le=5)
    agent_jngen_documents_per_round: int = Field(default=2, ge=1, le=3)
    agent_jngen_document_context_chars: int = Field(default=64_000, ge=8_000, le=100_000)
    agent_trial_seeds_per_subtask: int = Field(default=1, ge=1, le=5)
    agent_allow_legacy_keyword_routing: bool = False

    docker_host: str | None = Field(default=None, validation_alias="DOCKER_HOST")


@lru_cache
def get_settings() -> Settings:
    return Settings()
