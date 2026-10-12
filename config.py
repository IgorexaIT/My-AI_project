from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

# На Windows база IANA time zones может отсутствовать. Пакет tzdata
# добавлен в requirements.txt; фиксированный UTC+3 оставлен как fallback.
try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")

@dataclass(frozen=True)
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    database_path: str = os.getenv("DATABASE_PATH", "mai_lifeos.sqlite3")
    monthly_budget_default: float = float(os.getenv("MONTHLY_BUDGET_DEFAULT", "45000"))
    max_users: int = int(os.getenv("MAX_USERS", "10"))
    hh_user_agent: str = os.getenv("HH_USER_AGENT", "MAI-LifeOS-Bot/1.0 (student project)")
    gift_owner_id: int = int(os.getenv("GIFT_OWNER_ID", "1844256493"))
    gift_allowed_user_ids: str = os.getenv("GIFT_ALLOWED_USER_IDS", "")

    def can_use_gift_module(self, user_id: int) -> bool:
        """Only the owner and explicitly configured Telegram IDs may send gifts."""
        if user_id == self.gift_owner_id:
            return True
        allowed_ids: set[int] = set()
        for item in self.gift_allowed_user_ids.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                allowed_ids.add(int(item))
            except ValueError:
                # Ignore malformed IDs instead of preventing bot startup.
                continue
        return user_id in allowed_ids

config = Config()
