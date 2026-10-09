import os
from typing import List, Optional, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: Union[List[int], str] = []
    STORAGE_CHANNEL_ID: int
    UPDATES_CHANNEL_ID: Optional[int] = None
    DATABASE_URL: Optional[str] = None
    DB_PATH: str = "data/library.db"
    BOT_USERNAME: Optional[str] = None
    TELEGRAM_API_ID: Optional[int] = None
    TELEGRAM_API_HASH: Optional[str] = None
    TELEGRAM_PHONE: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: Union[str, List[int], int]) -> List[int]:
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, str):
            clean = v.strip()
            if not clean:
                return []
            return [
                int(item.strip())
                for item in clean.split(",")
                if item.strip().isdigit() or (item.strip().startswith("-") and item.strip()[1:].isdigit())
            ]
        return []

    @field_validator("STORAGE_CHANNEL_ID", mode="before")
    @classmethod
    def parse_storage_channel_id(cls, v: Union[str, int]) -> int:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("STORAGE_CHANNEL_ID must be specified.")
        if isinstance(v, str):
            v = v.strip()
        return int(v)

    @field_validator("UPDATES_CHANNEL_ID", mode="before")
    @classmethod
    def parse_updates_channel_id(cls, v: Optional[Union[str, int]]) -> Optional[int]:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, str):
            v = v.strip()
        return int(v)

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def parse_database_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        clean = str(v).strip()
        return clean if clean else None

    @field_validator("TELEGRAM_API_ID", mode="before")
    @classmethod
    def parse_api_id(cls, v: Optional[Union[str, int]]) -> Optional[int]:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return int(v)

    @field_validator("TELEGRAM_API_HASH", mode="before")
    @classmethod
    def parse_api_hash(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        clean = str(v).strip()
        return clean if clean else None


settings = Settings()
# Export alias config for compatibility
config = settings
