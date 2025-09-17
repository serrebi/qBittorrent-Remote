"""Persistence helpers for saving user preferences locally."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from qbittorrent_client import ConnectionSettings


_BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = str(_BASE_DIR / "qbittorrent-wx-client.json")


@dataclass
class AppSettings:
    connection: ConnectionSettings
    refresh_seconds: int = 5
    auto_refresh: bool = True
    default_filter: str = "all"
    confirm_delete: bool = True
    configured: bool = field(default=False, compare=False)


def load_settings() -> AppSettings:
    if not os.path.exists(SETTINGS_FILE):
        return AppSettings(connection=ConnectionSettings())
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            connection_data = data.get("connection", {})
            connection = ConnectionSettings(**connection_data)
            refresh = int(data.get("refresh_seconds", 5))
            auto_refresh = bool(data.get("auto_refresh", True))
            default_filter = data.get("default_filter", "all")
            confirm_delete = bool(data.get("confirm_delete", True))
            settings = AppSettings(
                connection=connection,
                refresh_seconds=refresh,
                auto_refresh=auto_refresh,
                default_filter=str(default_filter),
                confirm_delete=confirm_delete,
                configured=True,
            )
            return settings
    except Exception:
        return AppSettings(connection=ConnectionSettings())


def save_settings(settings: AppSettings) -> Optional[str]:
    payload = {
        "connection": asdict(settings.connection),
        "refresh_seconds": settings.refresh_seconds,
        "auto_refresh": settings.auto_refresh,
        "default_filter": settings.default_filter,
        "confirm_delete": settings.confirm_delete,
    }
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        return str(exc)
    settings.configured = True
    return None
