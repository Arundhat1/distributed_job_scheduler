"""
Centralized application configuration.

All environment-dependent values are read once here so the rest of the
codebase never touches os.environ directly. This keeps configuration
testable (override via env vars or a .env file) and makes it obvious
where to look when deploying to a new environment.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str = "postgresql+asyncpg://scheduler:scheduler@localhost:5432/scheduler"
    database_url_sync: str = "postgresql+psycopg2://scheduler:scheduler@localhost:5432/scheduler"

    # --- Auth ---
    jwt_secret: str = "CHANGE_ME_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8

    # --- Worker / Scheduler tuning ---
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 5.0
    worker_stale_after_seconds: int = 30  # heartbeat older than this -> worker considered dead
    scheduler_tick_seconds: float = 1.0
    default_job_timeout_seconds: int = 300
    default_max_retries: int = 3

    # --- Worker service auth ---
    # Shared secret workers present as `Authorization: Bearer <token>` on
    # every /api/v1/workers/* call (see app/worker_auth.py). Deliberately
    # has NO usable default (unlike jwt_secret's dev fallback) so a
    # misconfigured deployment fails closed instead of silently accepting
    # an empty/guessable worker token.
    scheduler_token: str = ""

    # --- App ---
    environment: str = "development"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()