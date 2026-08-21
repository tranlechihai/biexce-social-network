"""Application configuration via environment variables.

All sensitive values (signing secret, etc.) must be provided at runtime
via environment variables — never hard-coded or committed to source.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration for Ting Ting."""

    # Database
    database_url: str = "sqlite:///./ting_ting.db"

    # JWT / Auth
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    # Server-side session lifetime (refresh keeps the session alive within it).
    session_expire_days: int = 7

    # Cookie settings
    cookie_secure: bool = False  # True for staging HTTPS, False for local HTTP

    # Upload storage directory (relative values resolve against the CWD).
    # The container sets TING_UPLOADS_DIR to the mounted volume (/app/uploads).
    uploads_dir: str = "uploads"

    # Upload storage quotas (measured on real disk usage of uploads/)
    upload_quota_mb: float = 512.0
    total_upload_quota_mb: float = 5120.0

    # Server
    debug: bool = False
    rate_limit_enabled: bool = True

    model_config = {"env_prefix": "TING_", "extra": "ignore"}

    @model_validator(mode="after")
    def _validate_jwt_secret(self):
        if not self.jwt_secret:
            raise ValueError(
                "TING_JWT_SECRET must be set in the environment. "
                "Do not commit a real secret to source control."
            )
        return self

    @staticmethod
    def _redact(url: str) -> str:
        """Return database URL with any password redacted for safe logging."""
        if "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        # sqlite:///path has no password, pass through
        if scheme == "sqlite":
            return url
        return f"{scheme}://***redacted***"


_default_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global settings instance (lazy-loaded)."""
    global _default_settings
    if _default_settings is None:
        _default_settings = Settings()
    return _default_settings
