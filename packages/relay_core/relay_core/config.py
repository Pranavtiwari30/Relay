"""Typed runtime configuration.

Every setting is read from a ``RELAY_``-prefixed environment variable.
Validation runs at process start so a misconfigured deployment fails fast
instead of failing on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    # Read from the process environment only (twelve-factor). Local tooling loads
    # .env into the environment (`uv run --env-file .env`), which also makes the
    # RELAY_SECRET_* values visible to the secret resolver.
    model_config = SettingsConfigDict(env_prefix="RELAY_", extra="ignore")

    # --- runtime profile -------------------------------------------------
    env: Literal["local", "test", "cloud"] = "local"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- persistence -----------------------------------------------------
    database_url: str = "postgresql+psycopg://relay@127.0.0.1:54329/relay"
    db_pool_size: int = Field(default=5, ge=1, le=20)
    payload_retention_days: int = Field(default=30, ge=0, le=3650)

    # --- HTTP edge -------------------------------------------------------
    cors_allowed_origins: CsvList = ["http://localhost:5173"]
    max_webhook_body_bytes: int = Field(default=262_144, ge=1024, le=5_242_880)
    max_api_body_bytes: int = Field(default=1_048_576, ge=1024, le=5_242_880)
    webhook_rate_limit_per_minute: int = Field(default=120, ge=1)
    login_rate_limit_per_15_minutes: int = Field(default=10, ge=1)
    signature_tolerance_seconds: int = Field(default=300, ge=30, le=3600)

    # --- sessions --------------------------------------------------------
    session_cookie_name: str = "relay_session"
    session_ttl_hours: int = Field(default=12, ge=1, le=720)
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # --- messaging -------------------------------------------------------
    queue_backend: Literal["local", "pubsub", "memory"] = "local"
    pubsub_project_id: str | None = None
    pubsub_topic: str = "relay-deliveries"
    pubsub_publish_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    inline_publish: bool = True
    worker_base_url: str = "http://127.0.0.1:8001"
    background_loops: bool = False
    dispatch_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    outbox_lease_seconds: int = Field(default=30, ge=5, le=600)
    processing_lease_margin_seconds: int = Field(default=30, ge=5, le=600)
    local_queue_max_delivery_attempts: int = Field(default=5, ge=1, le=100)

    # --- internal (service-to-service) authentication ---------------------
    internal_auth_mode: Literal["token", "oidc"] = "token"
    internal_token: SecretStr | None = None
    internal_oidc_audience: str | None = None
    internal_oidc_service_accounts: CsvList = []

    # --- destinations ----------------------------------------------------
    destination_allow_http: bool = False
    destination_allow_private_hosts: CsvList = []
    destination_max_response_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    demo_destination_base_url: str = "http://127.0.0.1:8000"

    # --- AI investigation (optional; never used for normal processing) ----
    ai_providers: CsvList = []
    ai_max_steps: int = Field(default=6, ge=1, le=12)
    ai_request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    ai_total_timeout_seconds: float = Field(default=60.0, gt=0, le=240)
    # Ceiling per model response. Reasoning models spend part of this on thinking,
    # so a low value truncates the answer rather than saving money.
    ai_max_output_tokens: int = Field(default=16_000, ge=1024, le=64_000)
    ai_max_tool_result_chars: int = Field(default=6000, ge=500, le=50_000)
    ai_ollama_base_url: str = "http://127.0.0.1:11434"
    ai_ollama_model: str = "llama3.1"
    ai_openai_compat_base_url: str | None = None
    ai_openai_compat_api_key: SecretStr | None = None
    ai_openai_compat_model: str | None = None
    ai_anthropic_api_key: SecretStr | None = None
    ai_anthropic_model: str = "claude-opus-5-5"
    ai_anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    # --- MCP server ------------------------------------------------------
    mcp_workspace_id: str | None = None

    @field_validator(
        "cors_allowed_origins",
        "destination_allow_private_hosts",
        "internal_oidc_service_accounts",
        "ai_providers",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ai_providers")
    @classmethod
    def _known_providers(cls, value: list[str]) -> list[str]:
        known = {"ollama", "openai_compat", "anthropic"}
        unknown = [name for name in value if name not in known]
        if unknown:
            raise ValueError(
                f"unknown AI provider(s) {unknown}; expected a subset of {sorted(known)}"
            )
        return value

    @model_validator(mode="after")
    def _check_profile(self) -> Settings:
        problems: list[str] = []
        if "*" in self.cors_allowed_origins:
            problems.append("RELAY_CORS_ALLOWED_ORIGINS must be an explicit allowlist, not '*'")
        if self.session_cookie_samesite == "none" and not self.session_cookie_secure:
            problems.append("SameSite=None cookies require RELAY_SESSION_COOKIE_SECURE=true")
        if self.internal_auth_mode == "oidc" and not self.internal_oidc_audience:
            problems.append(
                "RELAY_INTERNAL_OIDC_AUDIENCE is required when internal auth mode is oidc"
            )
        if self.queue_backend == "pubsub" and not self.pubsub_project_id:
            problems.append("RELAY_PUBSUB_PROJECT_ID is required when the queue backend is pubsub")
        if self.env == "cloud":
            if not self.session_cookie_secure:
                problems.append("cloud profile requires RELAY_SESSION_COOKIE_SECURE=true")
            if self.destination_allow_http or self.destination_allow_private_hosts:
                problems.append(
                    "cloud profile forbids RELAY_DESTINATION_ALLOW_HTTP and "
                    "RELAY_DESTINATION_ALLOW_PRIVATE_HOSTS (SSRF dev overrides)"
                )
            if self.queue_backend != "pubsub":
                problems.append("cloud profile requires RELAY_QUEUE_BACKEND=pubsub")
            if self.internal_auth_mode == "token" and self.internal_token is None:
                problems.append("cloud profile requires internal authentication to be configured")
            if self.background_loops:
                problems.append(
                    "cloud profile forbids RELAY_BACKGROUND_LOOPS; Cloud Scheduler drives the dispatcher"
                )
        if problems:
            raise ValueError("invalid configuration: " + "; ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
