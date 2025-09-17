"""High-level qBittorrent Web API client used by the wxPython UI.

This module encapsulates the HTTP operations needed to interact with a
remote qBittorrent instance. The functions wrap the v2 Web API that ships
with modern qBittorrent releases.
"""
from __future__ import annotations

import json
import mimetypes
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import requests


class QBittorrentAPIError(RuntimeError):
    """Raised when the qBittorrent API returns an error."""


@dataclass
class ConnectionSettings:
    """Stores the base information required to connect to qBittorrent."""

    host: str = "http://127.0.0.1:8080"
    username: str = "admin"
    password: str = "adminadmin"
    verify_ssl: bool = True
    timeout: int = 15

    def normalized_url(self) -> str:
        host = self.host.rstrip("/")
        if host.endswith("/api/v2"):
            return host
        return f"{host}/api/v2"


@dataclass
class TorrentDetail:
    """Represents high level torrent info returned by /torrents/info."""

    infohash_v1: str
    name: str
    state: str
    progress: float
    dlspeed: int
    upspeed: int
    eta: int
    category: str
    ratio: float
    num_seeds: int
    num_leechs: int

    @classmethod
    def from_api(cls, payload: Dict) -> "TorrentDetail":
        return cls(
            infohash_v1=payload.get("hash", ""),
            name=payload.get("name", ""),
            state=payload.get("state", "unknown"),
            progress=payload.get("progress", 0.0),
            dlspeed=payload.get("dlspeed", 0),
            upspeed=payload.get("upspeed", 0),
            eta=payload.get("eta", -1),
            category=payload.get("category", ""),
            ratio=payload.get("ratio", 0.0),
            num_seeds=payload.get("num_seeds", 0),
            num_leechs=payload.get("num_leechs", 0),
        )


class QBittorrentClient:
    """Simple wrapper around qBittorrent's Web API."""

    def __init__(self, settings: Optional[ConnectionSettings] = None):
        self.settings = settings or ConnectionSettings()
        self._session = requests.Session()
        # qBittorrent uses cookie-based auth that we persist per session.
        self._is_authenticated = False

    # ------------------------------------------------------------------
    # Internal helpers

    def _api_url(self, path: str) -> str:
        base = self.settings.normalized_url().rstrip("/")
        path = path.lstrip("/")
        return f"{base}/{path}"

    def _request(self, method: str, path: str, **kwargs):
        url = self._api_url(path)
        kwargs.setdefault("timeout", self.settings.timeout)
        kwargs.setdefault("verify", self.settings.verify_ssl)
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code == 403:
            self._is_authenticated = False
            raise QBittorrentAPIError("Authentication failed or session expired")
        if resp.status_code >= 400:
            raise QBittorrentAPIError(f"API request failed ({resp.status_code}) {resp.text}")
        return resp

    # ------------------------------------------------------------------
    # Authentication

    def login(self) -> None:
        data = {
            "username": self.settings.username,
            "password": self.settings.password,
        }
        resp = self._request("post", "auth/login", data=data)
        if "Ok." not in resp.text:
            raise QBittorrentAPIError("Invalid username or password")
        self._is_authenticated = True

    def logout(self) -> None:
        try:
            self._request("post", "auth/logout")
        finally:
            self._is_authenticated = False
            self._session.cookies.clear()

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    # ------------------------------------------------------------------
    # Torrent management

    def get_torrents(self, filter_name: str = "all") -> List[TorrentDetail]:
        params = {"filter": filter_name}
        resp = self._request("get", "torrents/info", params=params)
        torrents = json.loads(resp.text)
        return [TorrentDetail.from_api(t) for t in torrents]

    def get_categories(self) -> Dict[str, Dict]:
        resp = self._request("get", "torrents/categories")
        return resp.json()

    def add_torrent_by_url(
        self,
        url: str,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
    ) -> None:
        data = {"urls": url}
        if category:
            data["category"] = category
        if save_path:
            data["savepath"] = save_path
        self._request("post", "torrents/add", data=data)

    def add_torrent_file(
        self,
        file_path: str,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
    ) -> None:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        with open(file_path, "rb") as fh:
            files = {"torrents": (os.path.basename(file_path), fh, mime_type or "application/x-bittorrent")}
            data = {}
            if category:
                data["category"] = category
            if save_path:
                data["savepath"] = save_path
            self._request("post", "torrents/add", data=data, files=files)

    def pause_torrents(self, hashes: Iterable[str]) -> None:
        self._bulk_action("torrents/pause", hashes)

    def resume_torrents(self, hashes: Iterable[str]) -> None:
        self._bulk_action("torrents/resume", hashes)

    def recheck_torrents(self, hashes: Iterable[str]) -> None:
        self._bulk_action("torrents/recheck", hashes)

    def delete_torrents(self, hashes: Iterable[str], delete_files: bool = False) -> None:
        data = {
            "hashes": "|".join(hashes),
            "deleteFiles": "true" if delete_files else "false",
        }
        self._request("post", "torrents/delete", data=data)

    def increase_priority(self, hashes: Iterable[str]) -> None:
        self._bulk_action("torrents/increasePrio", hashes)

    def decrease_priority(self, hashes: Iterable[str]) -> None:
        self._bulk_action("torrents/decreasePrio", hashes)

    # ------------------------------------------------------------------
    # Torrent details

    def get_torrent_properties(self, torrent_hash: str) -> Dict:
        resp = self._request("get", "torrents/properties", params={"hash": torrent_hash})
        return resp.json()

    def get_torrent_trackers(self, torrent_hash: str) -> List[Dict]:
        resp = self._request("get", "torrents/trackers", params={"hash": torrent_hash})
        return resp.json()

    def get_torrent_files(self, torrent_hash: str) -> List[Dict]:
        resp = self._request("get", "torrents/files", params={"hash": torrent_hash})
        return resp.json()

    # ------------------------------------------------------------------

    def _bulk_action(self, path: str, hashes: Iterable[str]) -> None:
        joined = "|".join(hashes)
        if not joined:
            raise ValueError("At least one torrent hash is required")
        data = {"hashes": joined}
        self._request("post", path, data=data)


__all__ = [
    "QBittorrentClient",
    "QBittorrentAPIError",
    "ConnectionSettings",
    "TorrentDetail",
]
