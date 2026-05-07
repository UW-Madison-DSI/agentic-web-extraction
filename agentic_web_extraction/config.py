from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AWE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    provider: str = "openai"
    model_extract: str = "gpt-5.5"
    model_screen: str = "gpt-5.4-mini"
    normalize: bool = True
    follow_pdf: bool = True
    max_fetches: int = 10

    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias="OPENAI_BASE_URL",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
