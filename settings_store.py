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
    # Mirror of the active profile's connection for backward compatibility
    connection: ConnectionSettings
    refresh_seconds: int = 5
    auto_refresh: bool = True
    default_filter: str = "all"
    confirm_delete: bool = True
    configured: bool = field(default=False, compare=False)
    # Multi-profile support
    profiles: dict[str, ConnectionSettings] = field(default_factory=dict)
    active_profile: str = "Default"


def load_settings() -> AppSettings:
    if not os.path.exists(SETTINGS_FILE):
        default_conn = ConnectionSettings()
        return AppSettings(
            connection=default_conn,
            profiles={"Default": default_conn},
            active_profile="Default",
        )
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            # Load profiles if present; else fall back to single connection
            profiles_data = data.get("profiles") or {}
            profiles: dict[str, ConnectionSettings] = {}
            if isinstance(profiles_data, dict) and profiles_data:
                for name, conn_dict in profiles_data.items():
                    try:
                        profiles[str(name)] = ConnectionSettings(**(conn_dict or {}))
                    except Exception:
                        continue

            if not profiles:
                connection_data = data.get("connection", {})
                single = ConnectionSettings(**connection_data)
                profiles = {"Default": single}
                active_profile = "Default"
            else:
                active_profile = str(data.get("active_profile") or "Default")
                if active_profile not in profiles:
                    # Choose the first existing profile by saved order
                    active_profile = next(iter(profiles))

            # Mirror the active profile connection instance directly so edits propagate.
            connection = profiles[active_profile]
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
                profiles=profiles,
                active_profile=active_profile,
            )
            return settings
    except Exception:
        default_conn = ConnectionSettings()
        return AppSettings(connection=default_conn, profiles={"Default": default_conn}, active_profile="Default")


def save_settings(settings: AppSettings) -> Optional[str]:
    payload = {
        # Keep single-connection mirror for backward compatibility
        "connection": asdict(settings.connection),
        "refresh_seconds": settings.refresh_seconds,
        "auto_refresh": settings.auto_refresh,
        "default_filter": settings.default_filter,
        "confirm_delete": settings.confirm_delete,
        # Multi-profile payload
        "profiles": {name: asdict(conn) for name, conn in (settings.profiles or {}).items()},
        "active_profile": settings.active_profile,
    }
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        return str(exc)
    settings.configured = True
    return None
