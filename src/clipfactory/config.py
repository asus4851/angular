"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AI analysis
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # Storage
    database_url: str = "sqlite:///data/clipfactory.db"
    data_dir: Path = Path("data")
    export_dir: Path = Path("exports")

    # Security
    secret_key: str = ""
    api_key: str = ""

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = ""

    # Pipeline
    poll_interval_min: int = 30
    dry_run: bool = False
    worker_poll_sec: float = 2.0
    job_max_attempts: int = 4

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.sources_dir, self.clips_dir, self.export_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
