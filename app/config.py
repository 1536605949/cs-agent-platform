"""Typed application configuration.

All settings can be provided through environment variables or a local ``.env``
file. Every value has a development-safe default, which is why the project runs
without any configuration at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- application -------------------------------------------------------
    app_name: str = "AI Customer Service Platform"
    environment: str = "development"
    log_level: str = "INFO"
    app_port: int = 8000

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite:///./data/app.db"
    # Load demo accounts, orders and the FAQ knowledge base on first start so
    # the platform is usable immediately after cloning.
    auto_seed: bool = True

    # --- conversation engine ----------------------------------------------
    engine_mode: Literal["builtin", "dify"] = "builtin"

    # --- optional LLM ------------------------------------------------------
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0

    # --- optional Dify orchestration --------------------------------------
    dify_base_url: str = "https://api.dify.ai/v1"
    dify_api_key: str = ""
    dify_timeout_seconds: float = 60.0

    # --- authentication ----------------------------------------------------
    auth_mode: Literal["dev", "jwt"] = "dev"
    jwt_secret: str = "dev-only-change-me"
    jwt_issuer: str = "cs-agent-platform"
    jwt_expire_minutes: int = 720

    admin_username: str = "admin"
    admin_password: str = "admin123"

    # --- behaviour tuning --------------------------------------------------
    handoff_confidence_threshold: float = 0.35
    handoff_max_fallback_turns: int = 2
    faq_top_k: int = 3
    faq_min_score: float = 1.2
    rate_limit_per_minute: int = 60
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # --- derived helpers ---------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def use_builtin_engine(self) -> bool:
        return self.engine_mode == "builtin"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def static_dir(self) -> Path:
        return STATIC_DIR


_PLACEHOLDERS = {
    "",
    "dev-only-change-me",
    "change-me",
    "admin123",
    "change-this-to-a-long-random-secret",
}


def is_placeholder(value: str) -> bool:
    """Return True when a secret is empty or still a well-known placeholder."""
    value = (value or "").strip()
    return value in _PLACEHOLDERS or value.lower().startswith(("replace", "change-this", "changeme"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_settings(settings: Settings | None = None) -> list[str]:
    """Fail fast on unsafe production configuration.

    Returns the list of problems (empty when the configuration is acceptable).
    Raises ``RuntimeError`` in production so the process refuses to start.
    """
    settings = settings or get_settings()
    if not settings.is_production:
        return []

    problems: list[str] = []
    if is_placeholder(settings.jwt_secret) or len(settings.jwt_secret) < 32:
        problems.append("JWT_SECRET must be a random secret of at least 32 characters")
    if is_placeholder(settings.admin_password):
        problems.append("ADMIN_PASSWORD must be changed from the default value")
    if settings.auth_mode != "jwt":
        problems.append("AUTH_MODE must be 'jwt' in production")
    if settings.engine_mode == "dify" and is_placeholder(settings.dify_api_key):
        problems.append("DIFY_API_KEY must be a real key when ENGINE_MODE=dify")
    if settings.llm_enabled and is_placeholder(settings.llm_api_key):
        problems.append("LLM_API_KEY must be a real key when LLM_ENABLED=true")

    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
    return problems
