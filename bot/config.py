"""Validated environment configuration; secrets never appear in repr/logs."""
from typing import Annotated
from zoneinfo import ZoneInfo
from datetime import time
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', hide_input_in_errors=True)
    discord_token: str = Field(min_length=1, repr=False)
    discord_proxy: str | None = Field(default=None, repr=False)
    guild_id: int | None = Field(default=None, gt=0)
    legacy_guild_id: int | None = Field(default=None, gt=0)
    moderator_role_ids: Annotated[set[int], NoDecode] = set()
    admin_role_ids: Annotated[set[int], NoDecode] = set()
    daily_report_channel_id: int | None = Field(default=None, gt=0)
    moderation_log_channel_id: int | None = Field(default=None, gt=0)
    database_url: str = 'sqlite+aiosqlite:///./data/rating.db'
    start_rating: int = 800
    daily_reward: int = 25
    punishment_amount: int = 50
    min_messages: int = 5
    payout_time: str = '18:00'
    timezone: str = 'Europe/Kyiv'
    log_level: str = 'INFO'
    message_retention_days: int = Field(default=35, ge=2)
    recovery_lookback_days: int = Field(default=35, ge=1)

    @field_validator('discord_proxy', mode='before')
    @classmethod
    def optional_proxy(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not value.startswith(('http://', 'https://')):
            raise ValueError('Discord proxy must use HTTP or HTTPS')
        return value

    @field_validator('guild_id', 'legacy_guild_id', 'daily_report_channel_id', 'moderation_log_channel_id', mode='before')
    @classmethod
    def optional_log_channel(cls, value):
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    @field_validator('moderator_role_ids', 'admin_role_ids', mode='before')
    @classmethod
    def roles(cls, value):
        if isinstance(value, str):
            value = {int(v.strip()) for v in value.split(',') if v.strip()}
        if any(int(v) <= 0 for v in value):
            raise ValueError('Role IDs must be positive')
        return value

    @field_validator('log_level')
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
            raise ValueError('Unsupported log level')
        return value

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator('payout_time')
    @classmethod
    def valid_time(cls, value):
        time.fromisoformat(value)
        return value

    @model_validator(mode='after')
    def validate_rules(self):
        if (self.start_rating, self.daily_reward, self.punishment_amount, self.min_messages) != (800,25,50,5):
            raise ValueError('Rules are fixed: 800 / 25 / 50 / 5')
        if self.payout_time != '18:00' or self.timezone != 'Europe/Kyiv':
            raise ValueError('Version 1.0 payout must be 18:00 Europe/Kyiv')
        if not self.database_url.startswith('sqlite+aiosqlite:///'):
            raise ValueError('Only async SQLite is supported in v1.0')
        if self.daily_report_channel_id is not None and self.daily_report_channel_id == self.moderation_log_channel_id:
            raise ValueError('Public report and private moderation channels must differ')
        if self.recovery_lookback_days > self.message_retention_days:
            raise ValueError('Recovery lookback cannot exceed deduplication retention')
        return self
