"""Настройки приложения: переменные окружения и .env."""

from __future__ import annotations

from datetime import date, timedelta, timezone

from pydantic_settings import BaseSettings, SettingsConfigDict

MSK = timezone(timedelta(hours=3), name="MSK")


class Settings(BaseSettings):
    """Конфигурация из окружения (см. .env.example)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = ""
    database_url: str = "postgresql://itmo:itmo@localhost:5432/itmo"
    redis_url: str | None = None
    # Telegram ID администратора — уведомления о новых пользователях.
    admin_tg_id: int | None = None
    poll_interval_seconds: int = 600
    # Дата окончания приёма оплат по договорам — горизонт прогноза.
    enroll_deadline: date = date(2026, 8, 20)
    # Дата окончания приёма заявлений — горизонт притока новых конкурентов.
    apply_deadline: date = date(2026, 8, 10)
    http_timeout: float = 30.0


settings = Settings()
