"""Application settings, loaded from environment variables / backend/.env (12-factor style).

Paths are anchored to the backend/ folder, so the app behaves the same no matter which
directory you start it from.
"""
import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent
FRONTEND_DIR = REPO_DIR / "frontend"
SECRETS_DIR = BACKEND_DIR / ".secrets"            # git-ignored; holds the auto-generated dev JWT secret
DEMO_ADMIN_PASSWORD = "ChangeMe!2026"

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "BriefDesk"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "text"
    log_file: str | None = None                # e.g. logs/briefdesk.log (rotated, 5 x 5 MB)

    # Storage. Relative paths resolve against backend/.
    database_url: str = "sqlite:///./briefdesk.db"
    checkpoint_db: str = "checkpoints.db"      # LangGraph state (pause / resume / revise)
    data_dir: str = "data"                     # exported files

    # Auth
    jwt_secret: str | None = Field(None, min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "briefdesk"
    access_token_minutes: int = 15             # short: the browser renews it with the httpOnly refresh cookie
    refresh_token_days: int = 7
    cookie_secure: bool | None = None          # None = on in production (HTTPS), off locally (http://127.0.0.1)
    bootstrap_admin_email: str = "admin@briefdesk.dev"
    bootstrap_admin_password: str = DEMO_ADMIN_PASSWORD
    allow_registration: bool = True
    bcrypt_rounds: int = Field(12, ge=4, le=15)
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    trusted_proxies: list[str] = []            # set when running behind a reverse proxy (X-Forwarded-For)

    # LLM: Groq. Each model has its own free-tier token budget, so agents are spread across models.
    llm_mode: Literal["groq", "fake"] = "groq"  # fake = scripted offline brain (tests, CI, demo without a key)
    groq_api_key: str | None = None
    model_router: str = "qwen/qwen3.8-27b"     # supervisor, chat, notes, verifier: short structured decisions
    model_fast: str = "openai/gpt-oss-20b"     # researcher (ReAct tool calling)
    model_large: str = "openai/gpt-oss-120b"   # planner, writer, reviewer, publisher
    model_tokens_per_minute: int = 8000        # Groq free tier, per model; the pacer waits instead of hitting 429
    llm_timeout_seconds: float = 60
    llm_max_attempts: int = Field(3, ge=1, le=5)
    llm_max_wait_seconds: float = 65           # longest we pace / honour Retry-After (> the 60 s window)
    llm_breaker_failures: int = 4
    llm_breaker_reset_seconds: int = 60
    llm_cache_ttl_seconds: int = 3600

    # Agents
    run_max_llm_calls: int = 45                # hard budget per run (one research / revision)
    run_max_iterations: int = 12               # supervisor steps per run
    max_sources_per_brief: int = 20
    research_steps: dict[str, int] = {"quick": 3, "standard": 5, "deep": 8}

    # Tools
    search_provider: Literal["ddgs", "fake"] = "ddgs"
    search_max_results: int = 6
    fetch_timeout_seconds: float = 12
    fetch_max_bytes: int = 3_000_000
    mcp_client_enabled: bool = True
    mcp_servers_file: str = "mcp_servers.json"  # relative to the repo root; see mcp_servers.example.json
    mcp_call_timeout_seconds: float = 30

    # Quotas
    max_concurrent_runs: int = 2               # worker threads (Groq free tier: keep this small)
    briefs_per_user_per_day: int = 30
    exports_per_user_per_hour: int = 40
    messages_per_minute: int = 20

    enable_docs: bool | None = None            # None = on, except when ENVIRONMENT=production

    @property
    def docs_enabled(self) -> bool:
        return self.enable_docs if self.enable_docs is not None else self.environment != "production"

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.environment == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        # "JWT_SECRET=" / "GROQ_API_KEY=" in .env mean "not set", not "empty string".
        if isinstance(data, dict):
            for key in list(data):
                if key.lower() in ("jwt_secret", "groq_api_key", "log_file", "enable_docs", "cookie_secure") \
                        and data[key] == "":
                    data[key] = None
        return data

    @model_validator(mode="after")
    def _finalise(self):
        if self.environment == "production":
            if not self.jwt_secret or self.jwt_secret.startswith("change-me"):
                raise ValueError("Set a strong JWT_SECRET (32+ chars) in production")
            if self.bootstrap_admin_password == DEMO_ADMIN_PASSWORD:
                raise ValueError("Set BOOTSTRAP_ADMIN_PASSWORD in production")
            if self.llm_mode == "fake":
                raise ValueError("LLM_MODE=fake is for tests and demos, not production")
        if not self.jwt_secret or self.jwt_secret.startswith("change-me"):
            self.jwt_secret = _local_jwt_secret(self.environment)
        self.database_url = _anchor_sqlite(self.database_url)
        self.checkpoint_db = _anchor(self.checkpoint_db)
        self.data_dir = _anchor(self.data_dir)
        return self

    @property
    def mcp_config_path(self) -> Path:
        p = Path(self.mcp_servers_file)
        return p if p.is_absolute() else REPO_DIR / p


def _anchor(path: str) -> str:
    if path == ":memory:" or Path(path).is_absolute():
        return path
    return str((BACKEND_DIR / path).resolve())


def _anchor_sqlite(url: str) -> str:
    """sqlite:///./x.db -> sqlite:///<backend>/x.db (absolute), so the working directory does not matter."""
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url == "sqlite:///:memory:":
        return url
    return prefix + Path(_anchor(url[len(prefix):])).as_posix()


def _local_jwt_secret(environment: str) -> str:
    """Never sign tokens with a secret that is published on GitHub.

    Tests get a throwaway secret; local dev gets a random one persisted in backend/.secrets/
    so logins survive restarts.
    """
    if environment == "test":
        return secrets.token_urlsafe(48)
    path = SECRETS_DIR / "jwt_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    SECRETS_DIR.mkdir(exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    log.warning("Generated a local JWT secret in %s (set JWT_SECRET to override)", path)
    return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
