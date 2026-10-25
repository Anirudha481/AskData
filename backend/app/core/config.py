"""
config.py
All configuration read from environment variables (via .env file).
Import `settings` anywhere in the app; never read os.environ directly.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database (admin – used by schema reader and benchmark runner) ──────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "northwind"
    db_user: str = "postgres"
    db_password: str = "postgres"

    # ── Database (read-only – used by query executor) ──────────────────────
    db_readonly_user: str = "askdata_reader"
    db_readonly_password: str = "askdata_readonly_pw"

    # ── LLM ───────────────────────────────────────────────────────────────
    llm_provider: str = "openai"       # "openai" | "google" | "anthropic" | "groq"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0       # 0 = deterministic, better for SQL
    llm_max_tokens: int = 1024

    # Keys – read from env; never hard-coded
    openai_api_key: str = ""
    google_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    # ── Safety limits ─────────────────────────────────────────────────────
    query_timeout_seconds: int = 10
    query_row_limit: int = 1000

    # ── Paths ─────────────────────────────────────────────────────────────
    schema_config_path: str = "schema_config.yml"
    audit_log_path: str = "../logs/audit.log"

    # ── Derived DSN helpers ───────────────────────────────────────────────
    @property
    def admin_dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def readonly_dsn(self) -> str:
        return (
            f"postgresql://{self.db_readonly_user}:{self.db_readonly_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


# Singleton – import this everywhere
settings = Settings()
