"""OS-specific helpers for registering .torrent and magnet handlers."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

try:  # pragma: no cover - platform specific
    import winreg  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

APP_NAME = "qBittorrent Remote"
APP_ID = "qbittorrent_remote_accessible"


def register_associations(target: Path, python_executable: Optional[str] = None) -> Tuple[bool, str]:
    """Register .torrent and magnet associations for Windows or Linux."""
    target = target.resolve()
    if sys.platform.startswith("win"):
        return _register_windows(target, python_executable)
    if sys.platform.startswith("linux"):
        return _register_linux(target, python_executable)
    return False, "File association support is available only on Windows and Linux."


def unregister_associations(target: Optional[Path] = None) -> Tuple[bool, str]:
    """Remove associations previously registered by this app."""
    path = target.resolve() if target else None
    if sys.platform.startswith("win"):
        return _unregister_windows(path)
    if sys.platform.startswith("linux"):
        return _unregister_linux()
    return False, "File association removal is available only on Windows and Linux."


# ---------------------------------------------------------------------------
# Windows helpers


def _build_command(target: Path, python_executable: Optional[str], placeholder: str) -> str:
    if target.suffix.lower() in {".exe", ".bat", ".cmd"} or os.access(target, os.X_OK):
        return f'"{target}" {placeholder}'
    executable = python_executable or sys.executable
    return f'"{executable}" "{target}" {placeholder}'


def _register_windows(target: Path, python_executable: Optional[str]) -> Tuple[bool, str]:  # pragma: no cover - Windows specific
    if winreg is None:
        return False, "winreg module unavailable on this platform."

    prog_id = f"{APP_ID}.torrent"
    command = _build_command(target, python_executable, "%1")

    try:
        _set_reg_value(winreg.HKEY_CURRENT_USER, r"Software\Classes\.torrent", None, prog_id)
        _set_reg_value(winreg.HKEY_CURRENT_USER, r"Software\Classes\.torrent", "Content Type", "application/x-bittorrent")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.torrent\OpenWithProgids") as key:
            try:
                winreg.SetValueEx(key, prog_id, 0, winreg.REG_NONE, b"")
            except OSError:
                pass
        _set_reg_value(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}", None, APP_NAME)
        _set_reg_value(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell", None, "open")
        _set_reg_value(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command", None, command)

        magnet_command = _build_command(target, python_executable, "%1")
        _set_reg_value(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet", None, APP_NAME)
        _set_reg_value(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet", "URL Protocol", "")
        _set_reg_value(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet\shell", None, "open")
        _set_reg_value(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet\shell\open\command", None, magnet_command)
    except OSError as exc:
        return False, f"Failed to update registry: {exc}"

    return True, "Registered .torrent and magnet handlers."


def _unregister_windows(target: Optional[Path]) -> Tuple[bool, str]:  # pragma: no cover - Windows specific
    if winreg is None:
        return False, "winreg module unavailable on this platform."

    prog_id = f"{APP_ID}.torrent"
    command_expected = None
    if target is not None:
        command_expected = _build_command(target, None, "%1")

    try:
        _delete_reg_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.torrent") as key:
            try:
                current, _ = winreg.QueryValueEx(key, None)
                if current == prog_id:
                    winreg.DeleteValue(key, None)
            except OSError:
                pass
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.torrent\OpenWithProgids") as key:
                winreg.DeleteValue(key, prog_id)
        except OSError:
            pass

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet\shell\open\command") as key:
            try:
                existing, _ = winreg.QueryValueEx(key, None)
                if command_expected is None or existing.strip().lower() == command_expected.strip().lower():
                    winreg.DeleteValue(key, None)
            except OSError:
                pass
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\magnet") as key:
            try:
                current, _ = winreg.QueryValueEx(key, None)
                if current == APP_NAME:
                    winreg.DeleteValue(key, None)
            except OSError:
                pass
    except OSError as exc:
        return False, f"Failed to update registry: {exc}"

    return True, "Removed registered handlers."


def _set_reg_value(root, path: str, name: Optional[str], value):  # pragma: no cover - Windows specific helper
    key = winreg.CreateKey(root, path)
    try:
        if isinstance(value, bytes):
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, value)
        else:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)


def _delete_reg_tree(root, path: str):  # pragma: no cover - Windows specific helper
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_reg_tree(root, f"{path}\\{sub}")
    except OSError:
        pass
    try:
        winreg.DeleteKey(root, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Linux helpers


def _register_linux(target: Path, python_executable: Optional[str]) -> Tuple[bool, str]:  # pragma: no cover - Linux specific
    applications_dir = Path.home() / ".local" / "share" / "applications"
    applications_dir.mkdir(parents=True, exist_ok=True)
    desktop_file = applications_dir / "qbittorrent-remote-accessible.desktop"

    exec_cmd = _build_linux_exec(target, python_executable)
    desktop_contents = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exec_cmd}
MimeType=application/x-bittorrent;x-scheme-handler/magnet;
NoDisplay=false
Terminal=false
Categories=Network;FileTransfer;
"""
    try:
        desktop_file.write_text(desktop_contents, encoding="utf-8")
    except OSError as exc:
        return False, f"Failed to write desktop file: {exc}"

    results = []
    for mime in ("application/x-bittorrent", "x-scheme-handler/magnet"):
        try:
            result = subprocess.run(
                ["xdg-mime", "default", desktop_file.name, mime],
                capture_output=True,
                text=True,
            )
            results.append(result.returncode == 0)
        except FileNotFoundError:
            return False, "xdg-mime command not found."

    try:
        subprocess.run(["update-desktop-database", str(applications_dir)], capture_output=True)
    except FileNotFoundError:
        pass

    if all(results):
        return True, "Registered .torrent and magnet handlers."
    return False, "xdg-mime could not set the required associations."


def _unregister_linux() -> Tuple[bool, str]:  # pragma: no cover - Linux specific
    desktop_file = Path.home() / ".local" / "share" / "applications" / "qbittorrent-remote-accessible.desktop"
    try:
        if desktop_file.exists():
            desktop_file.unlink()
    except OSError as exc:
        return False, f"Failed to remove desktop file: {exc}"

    for mime in ("application/x-bittorrent", "x-scheme-handler/magnet"):
        try:
            subprocess.run(["xdg-mime", "default", "xdg-open.desktop", mime], capture_output=True)
        except FileNotFoundError:
            return False, "xdg-mime command not found."

    return True, "Removed registered handlers."


def _build_linux_exec(target: Path, python_executable: Optional[str]) -> str:
    if os.access(target, os.X_OK) and target.suffix.lower() not in {".py", ".pyw"}:
        return f"{shlex.quote(str(target))} %u"
    executable = python_executable or sys.executable
    return f"{shlex.quote(executable)} {shlex.quote(str(target))} %u"
