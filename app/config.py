"""Configuration, read from environment variables (see the README for the full list).

App settings and database settings are separate classes so that DB-free code paths
(`/healthz`, `/version`, `alembic heads`) never require DB variables to be set.
"""

import re
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The ALB forwards `/a/...` unchanged, so the prefix must be a clean path: "" or "/a", "/a/b".
_PATH_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")

SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class AppSettings(BaseSettings):
    service_name: str = "service-b"
    env_name: str = "local"
    path_prefix: str = ""
    port: int = 8000
    # Baked into the image at build time (Docker build args), so /version reports
    # what is actually running rather than what someone thinks is running.
    git_sha: str = "unknown"
    git_branch: str = "unknown"

    @field_validator("path_prefix")
    @classmethod
    def _check_path_prefix(cls, value: str) -> str:
        # Fail loudly instead of guessing: "/a/" or "a" would silently mis-route behind the ALB.
        if not _PATH_PREFIX_RE.fullmatch(value):
            raise ValueError(
                f"PATH_PREFIX must be empty or look like '/a' (leading slash, no trailing "
                f"slash); got {value!r}"
            )
        return value


class DatabaseSettings(BaseSettings):
    """Connection settings for the service's own database (`DB_*`)."""

    model_config = SettingsConfigDict(env_prefix="DB_", validate_by_name=True)

    host: str
    port: int = 5432
    name: str
    user: str
    auth: Literal["iam", "password"] = "password"
    password: SecretStr | None = None
    sslmode: SslMode = "prefer"
    # Previews run smaller pools (2 + 3) than main (5 + 5): they share main's Aurora
    # cluster, and connections are what runs out first (D41).
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    # Shared by DB_* and SOURCE_DB_*: there is only one region per task.
    aws_region: str | None = Field(default=None, validation_alias="AWS_REGION")

    @model_validator(mode="after")
    def _check_auth(self) -> Self:
        if self.auth == "password" and self.password is None:
            raise ValueError("password auth needs a password (DB_PASSWORD / SOURCE_DB_PASSWORD)")
        if self.auth == "iam":
            if not self.aws_region:
                raise ValueError("IAM auth needs AWS_REGION to sign the auth token")
            # RDS rejects IAM auth tokens over non-SSL connections; catch it here, not at login.
            if self.sslmode not in ("require", "verify-ca", "verify-full"):
                raise ValueError(f"IAM auth needs sslmode require or stricter; got {self.sslmode}")
        return self


class SourceDatabaseSettings(DatabaseSettings):
    """The database `copy-db` reads from (`SOURCE_DB_*`): `main`'s DB, via a read-only role."""

    model_config = SettingsConfigDict(env_prefix="SOURCE_DB_")
