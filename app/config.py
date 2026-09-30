"""Настройки приложения. Значения читаются из окружения и файла .env."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Сервер ---
    # Поля названы по переменным окружения, поэтому в .env писать
    # TASKFLOW_HOST необязательно: работает и короткое HOST.
    host: str = Field(default="0.0.0.0", validation_alias="TASKFLOW_HOST")
    port: int = Field(default=8080, validation_alias="TASKFLOW_PORT")
    debug: bool = Field(default=False, validation_alias="TASKFLOW_DEBUG")
    root_level: str = Field(default="INFO", validation_alias="TASKFLOW_ROOT_LEVEL")

    # --- База данных ---
    database_url: str = "sqlite+aiosqlite:///./data/taskflow.db"
    db_echo: bool = False

    # --- Безопасность ---
    jwt_secret: str = ""
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    password_min_length: int = 8
    max_failed_logins: int = 10
    lockout_minutes: int = 15

    # --- CORS ---
    cors_origins: str = ""

    # --- Файлы ---
    max_upload_mb: int = 25
    storage_dir: str = "./data/storage"
    preview_max_edge: int = 1280

    # --- Голос ---
    voice_model: str = "base"
    voice_language: str = "ru"
    voice_enabled: bool = True
    voice_compute_type: str = "int8"
    models_dir: str = "./data/models"

    # --- Веб ---
    web_root: str = ""

    # --- Бэкапы ---
    backup_dir: str = "./data/backups"

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return "sqlite+aiosqlite:///./data/taskflow.db"
        if "://" not in value:
            value = f"sqlite+aiosqlite:///{value}"
        return value

    @field_validator("voice_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        allowed = {"tiny", "base", "small", "medium"}
        value = value.strip().lower()
        if value not in allowed:
            raise ValueError(f"VOICE_MODEL должна быть одной из: {', '.join(sorted(allowed))}")
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def path(self, value: str) -> Path:
        """Путь относительно корня проекта, если он не абсолютный."""
        p = Path(value)
        return p if p.is_absolute() else (BASE_DIR / p)

    @property
    def storage_path(self) -> Path:
        return self.path(self.storage_dir)

    @property
    def models_path(self) -> Path:
        return self.path(self.models_dir)

    @property
    def backups_path(self) -> Path:
        return self.path(self.backup_dir)

    @property
    def web_path(self) -> Path | None:
        return self.path(self.web_root) if self.web_root else None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for directory in (self.storage_path, self.models_path, self.backups_path):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve_jwt_secret(self) -> str:
        """Ключ из настроек, иначе из data/secret.key, иначе новый."""
        if self.jwt_secret:
            if len(self.jwt_secret) < 32 and not self.debug:
                raise ValueError(
                    "JWT_SECRET должен быть не короче 32 символов. "
                    "Сгенерируйте новый: python -c \"import secrets; "
                    'print(secrets.token_urlsafe(64))"'
                )
            return self.jwt_secret

        key_file = self.path("./data/secret.key")
        if key_file.exists():
            value = key_file.read_text(encoding="utf-8").strip()
            if value:
                return value

        value = secrets.token_urlsafe(64)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(value, encoding="utf-8")
        if not self.debug:
            key_file.chmod(0o600)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
