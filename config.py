import os
from typing import List, Optional, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: Union[List[int], str] = []
    STORAGE_CHANNEL_ID: int
    UPDATES_CHANNEL_ID: Optional[int] = None
    DB_PATH: str = "data/library.db"
    BOT_USERNAME: Optional[str] = None

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
            return [int(item.strip()) for item in clean.split(",") if item.strip().isdigit() or (item.strip().startswith("-") and item.strip()[1:].isdigit())]
        return []

    @field_validator("STORAGE_CHANNEL_ID", "UPDATES_CHANNEL_ID", mode="before")
    @classmethod
    def parse_channel_id(cls, v: Optional[Union[str, int]]) -> Optional[int]:
        if v is None or v == "":
            return None
        return int(v)


settings = Settings()
