"""Application configuration loader."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    timezone: str
    reminder_hour: int
    reminder_minute: int
    db_path: Path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    timezone = os.getenv("TIMEZONE", "Asia/Phnom_Penh").strip() or "Asia/Phnom_Penh"
    reminder_hour = int(os.getenv("REMINDER_HOUR", "7"))
    reminder_minute = int(os.getenv("REMINDER_MINUTE", "0"))

    default_db_path = project_root / "data" / "attendance.db"
    db_path = Path(os.getenv("DB_PATH", str(default_db_path))).expanduser()

    admin_ids: set[int] = set()
    for item in admin_ids_raw.split(","):
        item = item.strip()
        if item.isdigit():
            admin_ids.add(int(item))

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        timezone=timezone,
        reminder_hour=reminder_hour,
        reminder_minute=reminder_minute,
        db_path=db_path,
    )
