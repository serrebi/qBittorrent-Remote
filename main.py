"""wxPython based remote qBittorrent client focused on accessibility."""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import wx
import wx.adv

from qbittorrent_client import (
    ConnectionSettings,
    QBittorrentClient,
    TorrentDetail,
)
from settings_store import AppSettings, load_settings, save_settings
from file_associations import register_associations, unregister_associations

APP_TITLE = "qBittorrent Remote"
# Limit how many per-refresh tracker lookups we perform to reduce spikes.
MAX_TRACKER_LOOKUPS_PER_REFRESH = 40
# Built-in status filters supported by qBittorrent API.
FILTER_CHOICES = [
    "all",
    "downloading",
    "seeding",
    "completed",
    "paused",
    "resumed",
    "stalled",
    "errored",
]


def _get_app_icon(size=(32, 32)) -> wx.Icon:
    """Return a generic application icon sized for frame or tray usage."""
    icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, size)
    if icon.IsOk():
        return icon
    bitmap = wx.ArtProvider.GetBitmap(wx.ART_INFORMATION, wx.ART_OTHER, size)
    if bitmap.IsOk():
        icon = wx.Icon()
        icon.CopyFromBitmap(bitmap)
        return icon
    # Fallback to default empty icon to satisfy wx API requirements.
    return wx.Icon()


def normalize_open_item(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("magnet:?"):
        return text
    if text.startswith("file://"):
        parsed = urlparse(text)
        if sys.platform.startswith("win"):
            combined = (parsed.netloc or "") + (parsed.path or "")
            combined = combined.lstrip("/\\")
            path = Path(unquote(combined))
        else:
            combined = unquote(parsed.path or "")
            if parsed.netloc:
                combined = f"/{parsed.netloc}{combined}"
            path = Path(combined)
        return str(path)
    text = text.strip('"')
    path = Path(text).expanduser()
    if not path.is_absolute():
        try:
            path = path.resolve()
        except Exception:  # noqa: BLE001
            path = path.absolute()
    return str(path)


class _NamedAccessible(wx.Accessible):
    def __init__(self, name: str, role: int | None = None):
        super().__init__()
        self._name = name
        self._role = role

    def GetName(self, child_id: int):  # noqa: N802
        return wx.ACC_OK, self._name

    def GetRole(self, child_id: int):  # noqa: N802
        if self._role is not None:
            return wx.ACC_OK, self._role
        return wx.ACC_OK, wx.ROLE_SYSTEM_CLIENT


def set_accessible_label(control: wx.Window, description: str) -> None:
    """Attach accessibility metadata so screen readers announce the purpose."""
    control.SetName(description)
    try:
        control.SetToolTip(description)
    except Exception:  # noqa: BLE001
        pass
    try:
        control.SetHelpText(description)
    except Exception:  # noqa: BLE001
        pass
    if hasattr(control, "SetHint"):
        try:
            control.SetHint(description)
        except Exception:  # noqa: BLE001
            pass
    role = None
    if isinstance(control, wx.TextCtrl):
        role = wx.ROLE_SYSTEM_TEXT
    elif isinstance(control, wx.CheckBox):
        role = wx.ROLE_SYSTEM_CHECKBUTTON
    elif isinstance(control, wx.Choice):
        role = wx.ROLE_SYSTEM_COMBOBOX
    try:
        control.SetAccessible(_NamedAccessible(description, role))
    except Exception:  # noqa: BLE001
        pass


class ConnectionDialog(wx.Dialog):
    """Dialog that lets the user supply qBittorrent connection details."""

    def __init__(self, parent: wx.Window, settings: ConnectionSettings):
        super().__init__(parent, title="Connect to qBittorrent")
        self._host = wx.TextCtrl(self, value=settings.host)
        set_accessible_label(self._host, "Server address")
        self._username = wx.TextCtrl(self, value=settings.username)
        set_accessible_label(self._username, "Username")
        self._password = wx.TextCtrl(self, value=settings.password, style=wx.TE_PASSWORD)
        set_accessible_label(self._password, "Password")
        self._verify_ssl = wx.CheckBox(self, label="Verify TLS certificate")
        self._verify_ssl.SetValue(settings.verify_ssl)
        set_accessible_label(self._verify_ssl, "Verify TLS certificate")

        main_sizer = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=4, cols=2, hgap=8, vgap=6)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(self, label="Server address"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._host, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Username"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._username, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Password"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._password, 1, wx.EXPAND)

        grid.AddStretchSpacer()
        grid.Add(self._verify_ssl, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)

        main_sizer.Add(grid, 1, wx.ALL | wx.EXPAND, 14)

        button_sizer = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        main_sizer.Add(button_sizer, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizerAndFit(main_sizer)
        self._host.SetFocus()

    def get_settings(self, original: ConnectionSettings) -> ConnectionSettings:
        result = ConnectionSettings(
            host=self._host.GetValue().strip() or original.host,
            username=self._username.GetValue().strip(),
            password=self._password.GetValue(),
            verify_ssl=self._verify_ssl.GetValue(),
            timeout=original.timeout,
        )
        return result


class ProfileManagerDialog(wx.Dialog):
    """Manage multiple server profiles (add, remove, edit)."""

    def __init__(self, parent: wx.Window, profiles: Dict[str, ConnectionSettings], active: str):
        super().__init__(parent, title="Profiles")
        # Clone profiles so edits apply only on OK
        self._profiles: Dict[str, ConnectionSettings] = {
            name: ConnectionSettings(
                host=conn.host,
                username=conn.username,
                password=conn.password,
                verify_ssl=conn.verify_ssl,
                timeout=conn.timeout,
            )
            for name, conn in (profiles or {}).items()
        }
        if not self._profiles:
            self._profiles = {"Default": ConnectionSettings()}
        self._selected_name: str = active if active in self._profiles else next(iter(self._profiles))

        main = wx.BoxSizer(wx.VERTICAL)

        hsizer = wx.BoxSizer(wx.HORIZONTAL)
        # Left: list view of profiles (Name only)
        self._list = wx.ListCtrl(
            self,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_HRULES | wx.LC_VRULES,
            size=(320, -1),
        )
        # Name the control for screen readers without overriding native list accessibility
        try:
            self._list.SetName("Profiles list")
        except Exception:
            pass
        self._list.InsertColumn(0, "Name", width=300)
        hsizer.Add(self._list, 0, wx.ALL | wx.EXPAND, 8)

        # Right: editable fields for selected profile
        right = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=5, cols=2, hgap=8, vgap=6)
        grid.AddGrowableCol(1, 1)
        right.Add(grid, 1, wx.ALL | wx.EXPAND, 8)

        grid.Add(wx.StaticText(self, label="Server address"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._host = wx.TextCtrl(self)
        set_accessible_label(self._host, "Server address")
        grid.Add(self._host, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Username"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._username = wx.TextCtrl(self)
        set_accessible_label(self._username, "Username")
        grid.Add(self._username, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Password"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._password = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        set_accessible_label(self._password, "Password")
        grid.Add(self._password, 1, wx.EXPAND)

        self._verify_ssl = wx.CheckBox(self, label="Verify TLS certificate")
        set_accessible_label(self._verify_ssl, "Verify TLS certificate")
        grid.Add(self._verify_ssl, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)
        grid.AddSpacer(0)

        grid.Add(wx.StaticText(self, label="Request timeout (seconds)"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._timeout = wx.SpinCtrl(self, min=5, max=300, initial=15)
        set_accessible_label(self._timeout, "Request timeout in seconds")
        grid.Add(self._timeout, 0, wx.EXPAND)

        # Name field (profile display name) placed before the action buttons
        name_box = wx.BoxSizer(wx.HORIZONTAL)
        name_label = wx.StaticText(self, label="Name")
        self._name = wx.TextCtrl(self)
        set_accessible_label(self._name, "Profile name")
        name_box.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        name_box.Add(self._name, 1, wx.EXPAND)
        right.Add(name_box, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Buttons under fields
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self._add_btn = wx.Button(self, label="Add")
        self._remove_btn = wx.Button(self, label="Remove")
        self._up_btn = wx.Button(self, label="Move Up")
        self._down_btn = wx.Button(self, label="Move Down")
        set_accessible_label(self._add_btn, "Add profile")
        set_accessible_label(self._remove_btn, "Remove profile")
        set_accessible_label(self._up_btn, "Move profile up")
        set_accessible_label(self._down_btn, "Move profile down")
        btns.Add(self._add_btn, 0, wx.RIGHT, 6)
        btns.Add(self._remove_btn, 0, wx.RIGHT, 6)
        btns.Add(self._up_btn, 0, wx.RIGHT, 6)
        btns.Add(self._down_btn, 0)
        right.Add(btns, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        hsizer.Add(right, 1, wx.ALL | wx.EXPAND, 4)
        main.Add(hsizer, 1, wx.EXPAND)

        main.Add(self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizerAndFit(main)

        # Events
        self._list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_list_select)
        self._add_btn.Bind(wx.EVT_BUTTON, self._on_add)
        self._remove_btn.Bind(wx.EVT_BUTTON, self._on_remove)
        self._up_btn.Bind(wx.EVT_BUTTON, lambda evt: self._move_selected(-1))
        self._down_btn.Bind(wx.EVT_BUTTON, lambda evt: self._move_selected(1))
        self._name.Bind(wx.EVT_TEXT, self._on_name_change)
        self._host.Bind(wx.EVT_TEXT, self._on_field_change)
        self._username.Bind(wx.EVT_TEXT, self._on_field_change)
        self._password.Bind(wx.EVT_TEXT, self._on_field_change)
        self._verify_ssl.Bind(wx.EVT_CHECKBOX, self._on_field_change)
        self._timeout.Bind(wx.EVT_SPINCTRL, self._on_field_change)

        # Populate list initially and load the selected profile into fields
        self._refresh_list(select=self._selected_name)

    def _get_selected_name(self) -> Optional[str]:
        idx = self._list.GetFirstSelected()
        if idx == -1:
            return None
        return self._list.GetItemText(idx)

    def _load_selected_into_fields(self):
        name = self._get_selected_name()
        if not name:
            if self._list.GetItemCount():
                self._list.Select(0)
                self._list.Focus(0)
                name = self._list.GetItemText(0)
            else:
                return
        self._selected_name = name
        # Update the list's accessible name to include the selected profile
        try:
            self._list.SetName(f"Profiles list: {name}")
        except Exception:
            pass
        conn = self._profiles[name]
        # Load name field first so screen readers announce it clearly
        self._name.SetValue(name)
        self._host.SetValue(conn.host)
        self._username.SetValue(conn.username)
        self._password.SetValue(conn.password)
        self._verify_ssl.SetValue(conn.verify_ssl)
        try:
            self._timeout.SetValue(max(5, int(conn.timeout)))
        except Exception:
            self._timeout.SetValue(15)

    def _on_list_select(self, event):
        self._load_selected_into_fields()
        try:
            self._update_move_buttons()
        except Exception:
            pass

    def _on_field_change(self, event):
        name = self._selected_name
        if name not in self._profiles:
            return
        conn = self._profiles[name]
        conn.host = self._host.GetValue().strip() or conn.host
        conn.username = self._username.GetValue().strip()
        conn.password = self._password.GetValue()
        conn.verify_ssl = bool(self._verify_ssl.GetValue())
        conn.timeout = int(self._timeout.GetValue())
        self._update_selected_row_display()

    def _update_selected_row_display(self):
        idx = self._list.GetFirstSelected()
        if idx == -1:
            return
        name = self._list.GetItemText(idx)
        conn = self._profiles.get(name)
        if not conn:
            return
        # Column 0 is name
        self._list.SetItem(idx, 0, name)

    def _unique_name(self, base: str) -> str:
        idx = 1
        name = base
        existing = set(self._profiles.keys())
        while name in existing:
            idx += 1
            name = f"{base} {idx}"
        return name

    def _on_add(self, event):
        name = self._unique_name("Profile")
        # Seed new profile from current selection
        current = self._profiles.get(self._selected_name) or ConnectionSettings()
        self._profiles[name] = ConnectionSettings(
            host=current.host,
            username=current.username,
            password=current.password,
            verify_ssl=current.verify_ssl,
            timeout=current.timeout,
        )
        self._refresh_list(select=name)

    def _on_remove(self, event):
        if len(self._profiles) <= 1:
            wx.MessageBox("At least one profile is required.", APP_TITLE)
            return
        name = self._selected_name
        if name in self._profiles:
            del self._profiles[name]
        # Select the first remaining profile by order
        first = next(iter(self._profiles))
        self._refresh_list(select=first)

    def _on_name_change(self, event):
        # Rename the selected profile when the name field changes to a unique value
        old_name = self._selected_name
        if not old_name:
            return
        new_name = (self._name.GetValue() or "").strip()
        if not new_name or new_name == old_name:
            return
        if new_name in self._profiles:
            wx.MessageBox("A profile with that name already exists.", APP_TITLE)
            # Revert the field to the old name to keep state consistent
            self._name.ChangeValue(old_name)
            return
        # Perform rename by rekeying the dictionary
        self._profiles[new_name] = self._profiles.pop(old_name)
        self._selected_name = new_name
        self._refresh_list(select=new_name)

    def _refresh_list(self, select: Optional[str] = None):
        names = list(self._profiles.keys())
        self._list.DeleteAllItems()
        for name in names:
            idx = self._list.InsertItem(self._list.GetItemCount(), "")
            self._list.SetItem(idx, 0, name)
        # Select a row
        selected_index: Optional[int] = None
        if select and select in names:
            selected_index = names.index(select)
        elif names:
            selected_index = 0
        if selected_index is not None:
            self._list.Select(selected_index)
            self._list.Focus(selected_index)
        self._load_selected_into_fields()

        # Update move button enabled state
        try:
            self._update_move_buttons()
        except Exception:
            pass

    def _move_selected(self, delta: int):
        if not self._profiles:
            return
        names = list(self._profiles.keys())
        if self._selected_name not in names:
            return
        idx = names.index(self._selected_name)
        new_idx = max(0, min(len(names) - 1, idx + delta))
        if new_idx == idx:
            return
        # Reorder names and rebuild ordered dict
        names.pop(idx)
        names.insert(new_idx, self._selected_name)
        old_map = dict(self._profiles)
        self._profiles = {n: old_map[n] for n in names}
        self._refresh_list(select=self._selected_name)

    def _update_move_buttons(self):
        names = list(self._profiles.keys())
        if self._selected_name in names:
            idx = names.index(self._selected_name)
            self._up_btn.Enable(idx > 0)
            self._down_btn.Enable(idx < len(names) - 1)
        else:
            self._up_btn.Enable(False)
            self._down_btn.Enable(False)

    def get_profiles(self) -> Dict[str, ConnectionSettings]:
        return self._profiles

    def get_selected_name(self) -> Optional[str]:
        return self._selected_name


class AddTorrentDialog(wx.Dialog):
    """Dialog for providing a magnet link or HTTP(S) URL."""

    def __init__(self, parent: wx.Window, categories: Optional[List[str]] = None):
        super().__init__(parent, title="Add Torrent")
        self._magnet_ctrl = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        set_accessible_label(self._magnet_ctrl, "Magnet link or URL")

        self._category = wx.ComboBox(
            self,
            choices=sorted(categories) if categories else [],
            style=wx.CB_DROPDOWN,
        )
        set_accessible_label(self._category, "Category")
        self._save_path = wx.TextCtrl(self)
        set_accessible_label(self._save_path, "Save path")

        info = wx.StaticText(self, label="Paste a magnet link or URL to add a torrent remotely.")

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(info, 0, wx.ALL, 10)

        magnet_box = wx.StaticBoxSizer(wx.StaticBox(self, label="Magnet or URL"), wx.VERTICAL)
        magnet_box.Add(self._magnet_ctrl, 0, wx.ALL | wx.EXPAND, 6)
        main_sizer.Add(magnet_box, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)

        grid = wx.FlexGridSizer(rows=2, cols=2, hgap=6, vgap=6)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(self, label="Category"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._category, 0, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Save path"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._save_path, 0, wx.EXPAND)
        main_sizer.Add(grid, 0, wx.ALL | wx.EXPAND, 10)

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        main_sizer.Add(buttons, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizerAndFit(main_sizer)
        self._magnet_ctrl.SetFocus()

    @property
    def url(self) -> str:
        return self._magnet_ctrl.GetValue().strip()

    @property
    def category(self) -> str:
        return self._category.GetValue().strip()

    @property
    def save_path(self) -> str:
        return self._save_path.GetValue().strip()


class OptionsDialog(wx.Dialog):
    """Dialog for editing application preferences."""

    def __init__(self, parent: wx.Window, settings: AppSettings):
        super().__init__(parent, title="Options")
        self._original_settings = settings
        notebook = wx.Notebook(self)

        # General tab
        general_panel = wx.Panel(notebook)
        general_sizer = wx.BoxSizer(wx.VERTICAL)

        refresh_label = wx.StaticText(general_panel, label="Refresh interval (seconds)")
        self._refresh_spin = wx.SpinCtrl(
            general_panel,
            min=1,
            max=3600,
            initial=max(1, settings.refresh_seconds),
        )
        set_accessible_label(self._refresh_spin, "Refresh interval in seconds")
        general_sizer.Add(refresh_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        general_sizer.Add(self._refresh_spin, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 12)

        self._auto_refresh = wx.CheckBox(general_panel, label="Refresh torrent list automatically")
        self._auto_refresh.SetValue(settings.auto_refresh)
        set_accessible_label(self._auto_refresh, "Enable automatic refresh")
        general_sizer.Add(self._auto_refresh, 0, wx.ALL, 12)

        filter_label = wx.StaticText(general_panel, label="Default filter")
        self._filter_choice = wx.Choice(general_panel, choices=FILTER_CHOICES)
        set_accessible_label(self._filter_choice, "Default filter")
        default_filter = settings.default_filter if settings.default_filter in FILTER_CHOICES else FILTER_CHOICES[0]
        self._filter_choice.SetStringSelection(default_filter)
        general_sizer.Add(filter_label, 0, wx.LEFT | wx.RIGHT, 12)
        general_sizer.Add(self._filter_choice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        self._confirm_delete = wx.CheckBox(general_panel, label="Ask for confirmation before deleting torrents")
        self._confirm_delete.SetValue(settings.confirm_delete)
        set_accessible_label(self._confirm_delete, "Confirm before deleting torrents")
        general_sizer.Add(self._confirm_delete, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        general_panel.SetSizer(general_sizer)
        notebook.AddPage(general_panel, "General")

        # Connection tab
        connection_panel = wx.Panel(notebook)
        connection_sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=5, cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1, 1)

        host_label = wx.StaticText(connection_panel, label="Server address")
        self._host_ctrl = wx.TextCtrl(connection_panel, value=settings.connection.host)
        set_accessible_label(self._host_ctrl, "Server address")
        grid.Add(host_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._host_ctrl, 1, wx.EXPAND)

        user_label = wx.StaticText(connection_panel, label="Username")
        self._username_ctrl = wx.TextCtrl(connection_panel, value=settings.connection.username)
        set_accessible_label(self._username_ctrl, "Username")
        grid.Add(user_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._username_ctrl, 1, wx.EXPAND)

        password_label = wx.StaticText(connection_panel, label="Password")
        self._password_ctrl = wx.TextCtrl(connection_panel, value=settings.connection.password, style=wx.TE_PASSWORD)
        set_accessible_label(self._password_ctrl, "Password")
        grid.Add(password_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._password_ctrl, 1, wx.EXPAND)

        self._verify_ssl = wx.CheckBox(connection_panel, label="Verify TLS certificate")
        self._verify_ssl.SetValue(settings.connection.verify_ssl)
        set_accessible_label(self._verify_ssl, "Verify TLS certificate")
        grid.Add(self._verify_ssl, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)
        grid.AddSpacer(0)

        timeout_label = wx.StaticText(connection_panel, label="Request timeout (seconds)")
        self._timeout_spin = wx.SpinCtrl(
            connection_panel,
            min=5,
            max=300,
            initial=max(5, settings.connection.timeout),
        )
        set_accessible_label(self._timeout_spin, "Request timeout in seconds")
        grid.Add(timeout_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._timeout_spin, 0, wx.EXPAND)

        connection_sizer.Add(grid, 0, wx.ALL | wx.EXPAND, 12)
        connection_panel.SetSizer(connection_sizer)
        notebook.AddPage(connection_panel, "Connection (Active Profile)")

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(notebook, 1, wx.ALL | wx.EXPAND, 10)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        main_sizer.Add(buttons, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizerAndFit(main_sizer)
        notebook.SetFocus()

    def get_options(self) -> Dict:
        host_value = self._host_ctrl.GetValue().strip() or self._original_settings.connection.host
        return {
            "refresh_seconds": self._refresh_spin.GetValue(),
            "auto_refresh": self._auto_refresh.GetValue(),
            "default_filter": self._filter_choice.GetStringSelection(),
            "confirm_delete": self._confirm_delete.GetValue(),
            "connection": {
                "host": host_value,
                "username": self._username_ctrl.GetValue().strip(),
                "password": self._password_ctrl.GetValue(),
                "verify_ssl": self._verify_ssl.GetValue(),
                "timeout": self._timeout_spin.GetValue(),
            },
        }


class TorrentListCtrl(wx.ListCtrl):
    """Accessible list control for torrent overviews."""

    HEADERS = [
        ("Name", 300),
        ("State", 120),
        ("Progress", 90),
        ("Down", 90),
        ("Up", 90),
        ("ETA", 90),
        ("Category", 120),
        ("Ratio", 70),
        ("Seeds", 70),
        ("Peers", 70),
    ]

    def __init__(self, parent: wx.Window):
        super().__init__(
            parent,
            style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES,
        )
        for index, (title, width) in enumerate(self.HEADERS):
            self.InsertColumn(index, title)
            self.SetColumnWidth(index, width)
        self.SetMinSize((700, 280))
        self._torrents: List[TorrentDetail] = []

    def update_from(self, torrents: List[TorrentDetail]):
        # Remember selection and scroll position before rebuilding.
        self.Freeze()
        try:
            selected_hashes = set(self.get_selected_hashes())
            top_index = self.GetTopItem()
            top_hash = None
            if 0 <= top_index < len(self._torrents):
                top_hash = self._torrents[top_index].infohash_v1

            self._torrents = torrents
            self.DeleteAllItems()

            hash_to_index = {}
            for torrent in torrents:
                idx = self.InsertItem(self.GetItemCount(), torrent.name)
                self.SetItem(idx, 1, torrent.state)
                self.SetItem(idx, 2, f"{torrent.progress * 100:.1f}%")
                self.SetItem(idx, 3, format_speed(torrent.dlspeed))
                self.SetItem(idx, 4, format_speed(torrent.upspeed))
                self.SetItem(idx, 5, format_eta(torrent.eta))
                self.SetItem(idx, 6, torrent.category or "-")
                self.SetItem(idx, 7, f"{torrent.ratio:.2f}")
                self.SetItem(idx, 8, str(torrent.num_seeds))
                self.SetItem(idx, 9, str(torrent.num_leechs))
                self.SetItemData(idx, idx)
                hash_to_index[torrent.infohash_v1] = idx

            # Restore selection and focus to keep screen reader context stable.
            for infohash in selected_hashes:
                if infohash in hash_to_index:
                    idx = hash_to_index[infohash]
                    self.Select(idx)
                    self.Focus(idx)

            if top_hash and top_hash in hash_to_index:
                self.EnsureVisible(hash_to_index[top_hash])
        finally:
            self.Thaw()

    def get_selected(self) -> Optional[TorrentDetail]:
        index = self.GetFirstSelected()
        if index == -1:
            return None
        return self._torrents[index]

    def get_selected_hashes(self) -> List[str]:
        hashes: List[str] = []
        index = self.GetFirstSelected()
        while index != -1:
            hashes.append(self._torrents[index].infohash_v1)
            index = self.GetNextSelected(index)
        return hashes

    def select_all(self):
        for idx in range(self.GetItemCount()):
            self.Select(idx)
        if self.GetItemCount():
            self.Focus(0)


class DetailsDialog(wx.Dialog):
    """Shows properties, trackers and files for a given torrent."""

    def __init__(self, parent: wx.Window, torrent: TorrentDetail, client: QBittorrentClient):
        super().__init__(parent, title=f"Details - {torrent.name}")
        self._torrent = torrent
        self._client = client
        self._text = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            size=(600, 420),
        )
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self._text, 1, wx.ALL | wx.EXPAND, 10)
        button_sizer = self.CreateSeparatedButtonSizer(wx.CLOSE)
        main_sizer.Add(button_sizer, 0, wx.ALL | wx.EXPAND, 10)
        self.SetSizerAndFit(main_sizer)
        self._load_details()

    def _load_details(self):
        def worker():
            try:
                props = self._client.get_torrent_properties(self._torrent.infohash_v1)
                files = self._client.get_torrent_files(self._torrent.infohash_v1)
                trackers = self._client.get_torrent_trackers(self._torrent.infohash_v1)
                text_lines = ["Properties:"]
                for key, value in sorted(props.items()):
                    text_lines.append(f"  {key}: {value}")
                text_lines.append("\nFiles:")
                for item in files:
                    text_lines.append(f"  {item.get('name')} ({format_progress(item.get('progress', 0.0))})")
                text_lines.append("\nTrackers:")
                for tracker in trackers:
                    text_lines.append(f"  {tracker.get('url')} - {tracker.get('statusString')}")
                output = "\n".join(text_lines)
            except Exception as exc:  # noqa: BLE001
                output = f"Failed to load details: {exc}"
            wx.CallAfter(self._text.SetValue, output)

        threading.Thread(target=worker, daemon=True).start()


class SystemTrayIcon(wx.adv.TaskBarIcon):
    """Taskbar icon that lets the user restore or exit the application."""

    def __init__(self, frame: "MainFrame"):
        super().__init__()
        self._frame = frame
        self.SetIcon(_get_app_icon((16, 16)), APP_TITLE)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self._on_left_click)
        self.Bind(wx.adv.EVT_TASKBAR_RIGHT_UP, self._on_right_click)

    def _on_left_click(self, event):
        self._frame.restore_from_tray()

    def _on_right_click(self, event):
        menu = wx.Menu()
        restore_item = menu.Append(wx.ID_ANY, "Restore")
        options_item = menu.Append(wx.ID_PREFERENCES, "Options…")
        exit_item = menu.Append(wx.ID_EXIT, "Exit")
        menu.Bind(wx.EVT_MENU, self._on_restore, restore_item)
        menu.Bind(wx.EVT_MENU, self._on_options, options_item)
        menu.Bind(wx.EVT_MENU, self._on_exit, exit_item)
        try:
            self.PopupMenu(menu)
        finally:
            menu.Destroy()

    def _on_restore(self, event):
        self._frame.restore_from_tray()

    def _on_options(self, event):
        self._frame.restore_from_tray()
        self._frame._show_options_dialog()

    def _on_exit(self, event):
        self._frame.Close()

    def Destroy(self):  # noqa: N802 - wx override
        try:
            self.RemoveIcon()
        except Exception:  # noqa: BLE001
            pass
        return super().Destroy()


class MainFrame(wx.Frame):
    """Primary application window."""

    def __init__(self, pending_items: Optional[List[str]] = None):
        super().__init__(None, title=APP_TITLE, size=(900, 600))
        self._connect_menu_item: Optional[wx.MenuItem] = None
        self._disconnect_menu_item: Optional[wx.MenuItem] = None
        self._taskbar_icon: Optional[SystemTrayIcon] = None
        self.settings: AppSettings = load_settings()
        self.client = QBittorrentClient(self.settings.connection)
        self._is_connecting = False
        self._pending_items: List[str] = []
        # Tracker and filtering state
        self._tracker_map: Dict[str, set] = {}
        self._tracker_hosts: List[str] = []
        self._tracker_hosts_set: set = set()
        self._all_torrents: List[TorrentDetail] = []
        self._displayed_hashes: List[str] = []
        self._search_debounce = None  # type: Optional[wx.CallLater]
        # Profiles UI state
        self._profile_choice: Optional[wx.Choice] = None
        if pending_items:
            self._enqueue_open_items(pending_items, notify=False)

        self._build_ui()
        self.SetIcon(_get_app_icon())
        if wx.adv.TaskBarIcon.IsAvailable():
            self._taskbar_icon = SystemTrayIcon(self)
        self.Bind(wx.EVT_ICONIZE, self._on_iconize)
        self.Centre()

        wx.CallAfter(self._auto_connect_or_prompt)

    # ------------------------------------------------------------------

    def _build_ui(self):
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        self._connection_label = wx.StaticText(panel, label="Disconnected")
        vbox.Add(self._connection_label, 0, wx.ALL, 8)

        controls_sizer = wx.BoxSizer(wx.HORIZONTAL)
        # Profile switcher
        controls_sizer.Add(wx.StaticText(panel, label="Profile"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._profile_choice = wx.Choice(panel, choices=list(self.settings.profiles.keys()))
        set_accessible_label(self._profile_choice, "Server profile")
        if self.settings.active_profile in self.settings.profiles:
            self._profile_choice.SetStringSelection(self.settings.active_profile)
        elif self._profile_choice.GetCount():
            self._profile_choice.SetSelection(0)
        controls_sizer.Add(self._profile_choice, 0, wx.RIGHT, 10)

        self._filter_choice = wx.Choice(panel, choices=FILTER_CHOICES)
        set_accessible_label(self._filter_choice, "Torrent filter")
        default_filter = self.settings.default_filter if self.settings.default_filter in FILTER_CHOICES else FILTER_CHOICES[0]
        self._filter_choice.SetStringSelection(default_filter)
        controls_sizer.Add(wx.StaticText(panel, label="Filter"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        controls_sizer.Add(self._filter_choice, 0, wx.RIGHT, 10)
        # Search box (first Tab after list)
        self._search_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        set_accessible_label(self._search_ctrl, "Search torrents")
        controls_sizer.Add(wx.StaticText(panel, label="Search"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        controls_sizer.Add(self._search_ctrl, 1, wx.RIGHT | wx.EXPAND, 10)
        self._refresh_button = wx.Button(panel, label="Refresh")
        self._refresh_button.Bind(wx.EVT_BUTTON, lambda evt: self.refresh_torrents())
        controls_sizer.Add(self._refresh_button, 0, wx.RIGHT, 6)

        self._pause_button = wx.Button(panel, label="Pause")
        self._pause_button.Bind(wx.EVT_BUTTON, lambda evt: self._pause_selected())
        controls_sizer.Add(self._pause_button, 0, wx.RIGHT, 6)

        self._resume_button = wx.Button(panel, label="Resume")
        self._resume_button.Bind(wx.EVT_BUTTON, lambda evt: self._resume_selected())
        controls_sizer.Add(self._resume_button, 0, wx.RIGHT, 6)

        self._delete_button = wx.Button(panel, label="Delete")
        self._delete_button.Bind(wx.EVT_BUTTON, lambda evt: self._delete_selected(False))
        controls_sizer.Add(self._delete_button, 0, wx.RIGHT, 6)

        self._delete_with_data_button = wx.Button(panel, label="Delete + Data")
        self._delete_with_data_button.Bind(wx.EVT_BUTTON, lambda evt: self._delete_selected(True))
        controls_sizer.Add(self._delete_with_data_button, 0, wx.RIGHT, 6)

        self._recheck_button = wx.Button(panel, label="Recheck")
        self._recheck_button.Bind(wx.EVT_BUTTON, lambda evt: self._recheck_selected())
        controls_sizer.Add(self._recheck_button, 0, wx.RIGHT, 6)

        self._details_button = wx.Button(panel, label="Details…")
        self._details_button.Bind(wx.EVT_BUTTON, lambda evt: self._open_details())
        controls_sizer.Add(self._details_button, 0)

        vbox.Add(controls_sizer, 0, wx.ALL, 8)

        self._list = TorrentListCtrl(panel)
        vbox.Add(self._list, 1, wx.ALL | wx.EXPAND, 8)

        panel.SetSizer(vbox)

        self._build_menus()
        self._create_status_bar()
        self._setup_accelerators()

        self.Bind(wx.EVT_CHOICE, self._on_filter_changed, self._filter_choice)
        self.Bind(wx.EVT_CHOICE, self._on_profile_changed, self._profile_choice)
        self.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda evt: self._open_details(), self._list)
        self._list.Bind(wx.EVT_CONTEXT_MENU, self._on_list_context)
        self._list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self._on_list_item_right_click)
        # Live filtering on text change and Enter key
        self._search_ctrl.Bind(wx.EVT_TEXT, self._on_search_changed)
        self._search_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_search_changed)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda evt: self.refresh_torrents(False), self._timer)
        self._configure_timer()
        # Focus and tab order: list gets default focus, Tab goes to search.
        wx.CallAfter(self._list.SetFocus)
        self._search_ctrl.MoveAfterInTabOrder(self._list)

    def _build_menus(self):
        menubar = wx.MenuBar()

        file_menu = wx.Menu()
        connect_item = file_menu.Append(wx.ID_ANY, "Connect…\tCtrl+Shift+C")
        disconnect_item = file_menu.Append(wx.ID_ANY, "Disconnect")
        profiles_item = file_menu.Append(wx.ID_ANY, "Profiles…")
        file_menu.AppendSeparator()
        open_file_item = file_menu.Append(wx.ID_OPEN, "Open Torrent File…\tCtrl+O")
        add_item = file_menu.Append(wx.ID_ANY, "Add Torrent…\tCtrl+N")
        options_item = file_menu.Append(wx.ID_PREFERENCES, "Options…\tCtrl+Shift+O")
        minimize_item = file_menu.Append(wx.ID_ANY, "Minimize to Tray\tCtrl+M")
        file_menu.AppendSeparator()
        register_item = file_menu.Append(wx.ID_ANY, "Register File Associations")
        unregister_item = file_menu.Append(wx.ID_ANY, "Remove File Associations")
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, "Exit")
        menubar.Append(file_menu, "&File")

        action_menu = wx.Menu()
        select_all_item = action_menu.Append(wx.ID_SELECTALL, "Select All\tCtrl+A")
        pause_item = action_menu.Append(wx.ID_ANY, "Pause\tCtrl+P")
        resume_item = action_menu.Append(wx.ID_ANY, "Resume\tCtrl+R")
        delete_item = action_menu.Append(wx.ID_ANY, "Delete\tDel")
        delete_data_item = action_menu.Append(wx.ID_ANY, "Delete + Data\tShift+Del")
        recheck_item = action_menu.Append(wx.ID_ANY, "Recheck")
        action_menu.AppendSeparator()
        details_item = action_menu.Append(wx.ID_ANY, "Details…\tEnter")
        menubar.Append(action_menu, "&Actions")

        help_menu = wx.Menu()
        about_item = help_menu.Append(wx.ID_ABOUT, "About")
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)

        disconnect_item.Enable(False)

        self.Bind(wx.EVT_MENU, lambda evt: self._on_connect_menu(), connect_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._disconnect(), disconnect_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._open_profiles_manager(), profiles_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._open_torrent_files_dialog(), open_file_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._show_add_dialog(), add_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._show_options_dialog(), options_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._minimize_to_tray(), minimize_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._register_file_associations(), register_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._remove_file_associations(), unregister_item)
        self.Bind(wx.EVT_MENU, lambda evt: self.Close(), exit_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._on_select_all(), select_all_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._pause_selected(), pause_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._resume_selected(), resume_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._delete_selected(False), delete_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._delete_selected(True), delete_data_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._recheck_selected(), recheck_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._open_details(), details_item)
        self.Bind(wx.EVT_MENU, lambda evt: self._show_about(), about_item)

        self._connect_menu_item = connect_item
        self._disconnect_menu_item = disconnect_item

        # Cache menu ids for accelerator table creation.
        self._menu_ids = {
            "connect": connect_item.GetId(),
            "open_file": open_file_item.GetId(),
            "add": add_item.GetId(),
            "minimize": minimize_item.GetId(),
            "select_all": select_all_item.GetId(),
            "pause": pause_item.GetId(),
            "resume": resume_item.GetId(),
            "options": options_item.GetId(),
            "delete": delete_item.GetId(),
            "delete_data": delete_data_item.GetId(),
        }

    def _create_status_bar(self):
        status = self.CreateStatusBar()
        status.SetStatusText("Ready")

    def _setup_accelerators(self):
        entries = [
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("C"), self._menu_ids["connect"]),
            (wx.ACCEL_CTRL, ord("O"), self._menu_ids["open_file"]),
            (wx.ACCEL_CTRL, ord("N"), self._menu_ids["add"]),
            (wx.ACCEL_CTRL, ord("M"), self._menu_ids["minimize"]),
            (wx.ACCEL_CTRL, ord("A"), self._menu_ids["select_all"]),
            (wx.ACCEL_CTRL, ord("P"), self._menu_ids["pause"]),
            (wx.ACCEL_CTRL, ord("R"), self._menu_ids["resume"]),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("O"), self._menu_ids["options"]),
            (wx.ACCEL_NORMAL, wx.WXK_DELETE, self._menu_ids["delete"]),
            (wx.ACCEL_SHIFT, wx.WXK_DELETE, self._menu_ids["delete_data"]),
        ]
        accel_entries = [wx.AcceleratorEntry(*entry) for entry in entries]
        self.SetAcceleratorTable(wx.AcceleratorTable(accel_entries))

    def _on_iconize(self, event):
        if event.Iconized():
            self._minimize_to_tray()
        event.Skip()

    def restore_from_tray(self):
        if not self.IsShown():
            self.Show()
        if self.IsIconized():
            self.Iconize(False)
        self.Raise()
        self.SetFocus()

    def _minimize_to_tray(self):
        if not wx.adv.TaskBarIcon.IsAvailable() or self._taskbar_icon is None:
            self.Iconize(True)
            return
        if self.IsShown():
            self.Hide()
        if not self.IsIconized():
            # Ensure the frame enters an iconized state so restore works correctly.
            self.Iconize(True)
        self._set_status("Minimized to tray")

    # ------------------------------------------------------------------

    def _auto_connect_or_prompt(self):
        if self.settings.configured and self.settings.connection.host:
            self._connect(self.settings.connection)
        else:
            self._prompt_connect()

    def _configure_timer(self):
        interval = max(3, self.settings.refresh_seconds) * 1000
        if self.settings.auto_refresh:
            if self._timer.IsRunning():
                self._timer.Stop()
            self._timer.Start(interval)
        else:
            if self._timer.IsRunning():
                self._timer.Stop()

    def _on_connect_menu(self):
        if self.client.is_authenticated:
            self._set_status("Already connected")
            return
        self._prompt_connect()

    # ------------------------ Profiles -------------------------------
    def _populate_profile_choice(self):
        if not self._profile_choice:
            return
        names = list(self.settings.profiles.keys())
        self._profile_choice.Clear()
        self._profile_choice.AppendItems(names)
        if self.settings.active_profile in names:
            self._profile_choice.SetStringSelection(self.settings.active_profile)
        elif names:
            self._profile_choice.SetSelection(0)

    def _on_profile_changed(self, event):
        if not self._profile_choice:
            return
        new_name = self._profile_choice.GetStringSelection()
        if not new_name or new_name == self.settings.active_profile:
            return
        self._switch_profile(new_name)

    def _switch_profile(self, profile_name: str):
        if profile_name not in self.settings.profiles:
            self._show_error(f"Unknown profile '{profile_name}'")
            return
        # Stop refresh and disconnect current session
        try:
            self._timer.Stop()
        except Exception:
            pass
        if self.client.is_authenticated:
            try:
                self.client.logout()
            except Exception:
                pass
        # Clear UI and caches
        self._connection_label.SetLabel(f"Switching to '{profile_name}'…")
        self._list.DeleteAllItems()
        self._tracker_map.clear()
        self._tracker_hosts.clear()
        try:
            self._tracker_hosts_set.clear()
        except Exception:
            pass
        self._displayed_hashes.clear()
        self._all_torrents.clear()

        # Point settings to new active profile
        self.settings.active_profile = profile_name
        new_conn = self.settings.profiles[profile_name]
        self.settings.connection = new_conn  # keep mirror in sync
        save_settings(self.settings)

        # Build a fresh client for clean cookies/session
        self.client = QBittorrentClient(new_conn)
        self._is_connecting = False
        self._connect(new_conn)

    def _open_profiles_manager(self):
        dlg = ProfileManagerDialog(self, self.settings.profiles, self.settings.active_profile)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                new_profiles = dlg.get_profiles()
                # Apply possibly re-ordered/renamed profiles and switch to selection
                selected = dlg.get_selected_name() or self.settings.active_profile
                self.settings.profiles = new_profiles
                if selected not in self.settings.profiles:
                    # choose first by current order
                    selected = next(iter(self.settings.profiles))
                self.settings.active_profile = selected
                self.settings.connection = self.settings.profiles[selected]
                save_settings(self.settings)
                # Refresh profile selector
                self._populate_profile_choice()
                # Switch (reconnect) to active to ensure client matches
                self._switch_profile(selected)
        finally:
            dlg.Destroy()

    def _show_options_dialog(self):
        dlg = OptionsDialog(self, self.settings)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                options = dlg.get_options()
                self.settings.refresh_seconds = options["refresh_seconds"]
                self.settings.auto_refresh = options["auto_refresh"]
                self.settings.default_filter = options["default_filter"]
                self.settings.confirm_delete = options["confirm_delete"]
                connection_opts = options["connection"]
                self.settings.connection.host = connection_opts["host"]
                self.settings.connection.username = connection_opts["username"]
                self.settings.connection.password = connection_opts["password"]
                self.settings.connection.verify_ssl = connection_opts["verify_ssl"]
                self.settings.connection.timeout = connection_opts["timeout"]

                self.client.settings.host = self.settings.connection.host
                self.client.settings.username = self.settings.connection.username
                self.client.settings.password = self.settings.connection.password
                self.client.settings.verify_ssl = self.settings.connection.verify_ssl
                self.client.settings.timeout = self.settings.connection.timeout
                err = save_settings(self.settings)
                if err:
                    self._show_error(f"Failed to save options: {err}")
                else:
                    self._configure_timer()
                    filter_value = self.settings.default_filter if self.settings.default_filter in FILTER_CHOICES else FILTER_CHOICES[0]
                    self._filter_choice.SetStringSelection(filter_value)
                    self._set_status("Options updated")
        finally:
            dlg.Destroy()

    def _on_filter_changed(self, event):
        selection = self._filter_choice.GetStringSelection()
        # Persist only status filters as default; tracker filters are dynamic.
        if selection in FILTER_CHOICES:
            self.settings.default_filter = selection
            err = save_settings(self.settings)
            if err:
                self._set_status(f"Failed to save filter: {err}")
            # Status filter change requires new data from server.
            self.refresh_torrents()
        else:
            # Tracker-only change: reuse cached data, no network call.
            self._apply_client_side_filters(announce=True)

    def _on_search_changed(self, event):
        # Debounce to avoid rebuilding list on every keystroke.
        if self._search_debounce is not None:
            try:
                self._search_debounce.Stop()
            except Exception:
                pass
        self._search_debounce = wx.CallLater(200, self._apply_client_side_filters)

    def _on_select_all(self):
        self._list.select_all()

    def _open_torrent_files_dialog(self):
        with wx.FileDialog(
            self,
            message="Open torrent file",
            wildcard="Torrent files (*.torrent)|*.torrent|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                paths = dlg.GetPaths()
                self._enqueue_open_items(paths)

    def _enqueue_open_items(self, items: List[str], notify: bool = True):
        normalized: List[str] = []
        for raw in items:
            normalized_item = normalize_open_item(raw)
            if normalized_item:
                normalized.append(normalized_item)
        if not normalized:
            return
        self._pending_items.extend(normalized)
        if self.client.is_authenticated:
            self._process_pending_items()
        elif notify:
            self._set_status("Pending torrents will be added after connecting")

    def _process_pending_items(self):
        if not self.client.is_authenticated or not self._pending_items:
            return
        pending = list(self._pending_items)
        self._pending_items.clear()
        for item in pending:
            self._handle_incoming_item(item)

    def _handle_incoming_item(self, item: str):
        if item.lower().startswith("magnet:?"):
            self._add_torrent(item, "", "", "")
            return
        path = Path(item)
        if not path.exists():
            self._show_error(f"Cannot open '{item}': file not found")
            return
        self._add_torrent("", str(path), "", "")

    def _register_file_associations(self):
        success, message = register_associations(Path(sys.argv[0]).resolve(), sys.executable)
        if success:
            self._set_status("File associations registered")
            wx.MessageBox(message or "File associations registered.", APP_TITLE)
        else:
            self._show_error(message or "Failed to register file associations")

    def _remove_file_associations(self):
        success, message = unregister_associations(Path(sys.argv[0]).resolve())
        if success:
            self._set_status("File associations removed")
            wx.MessageBox(message or "File associations removed.", APP_TITLE)
        else:
            self._show_error(message or "Failed to remove file associations")

    def _on_list_item_right_click(self, event):
        index = event.GetIndex()
        if index != wx.NOT_FOUND:
            self._list.Select(index)
            self._list.Focus(index)
        self._show_list_context_menu(event.GetPoint())

    def _on_list_context(self, event: wx.ContextMenuEvent):
        pos = event.GetPosition()
        if pos == wx.DefaultPosition or (pos.x == -1 and pos.y == -1):
            client_pos = None
        else:
            client_pos = self._list.ScreenToClient(pos)
        self._show_list_context_menu(client_pos)

    def _show_list_context_menu(self, client_pos: Optional[wx.Point]):
        if not self.client.is_authenticated:
            self._announce_not_connected()
            return
        if client_pos is not None:
            item, _ = self._list.HitTest(client_pos)
            if item != wx.NOT_FOUND and not self._list.IsSelected(item):
                self._list.Select(item)
                self._list.Focus(item)
        if not self._list.get_selected_hashes():
            self._announce_require_selection()
            return

        menu = wx.Menu()
        remove_item = menu.Append(wx.ID_DELETE, "Remove")
        remove_data_item = menu.Append(wx.ID_ANY, "Remove with data")
        menu.Bind(wx.EVT_MENU, self._on_context_remove, remove_item)
        menu.Bind(wx.EVT_MENU, self._on_context_remove_with_data, remove_data_item)
        try:
            if client_pos is not None:
                self._list.PopupMenu(menu, client_pos)
            else:
                self._list.PopupMenu(menu)
        finally:
            menu.Destroy()

    def _on_context_remove(self, event):
        self._delete_selected(False)

    def _on_context_remove_with_data(self, event):
        self._delete_selected(True)

    def _prompt_connect(self):
        if self._is_connecting or self.client.is_authenticated:
            return
        dlg = ConnectionDialog(self, self.settings.connection)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                new_settings = dlg.get_settings(self.settings.connection)
                self._connect(new_settings)
        finally:
            dlg.Destroy()


    def _connect(self, settings: ConnectionSettings):
        if self._is_connecting:
            return
        self._is_connecting = True
        self._set_status("Connecting…")
        self._connection_label.SetLabel("Connecting…")

        def worker():
            client = QBittorrentClient(settings)
            try:
                client.login()
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._show_error, f"Could not connect: {exc}")
                wx.CallAfter(self._set_status, "Connection failed")
                wx.CallAfter(self._connection_label.SetLabel, "Disconnected")
            else:
                wx.CallAfter(self._apply_connection, client, settings)
            finally:
                self._is_connecting = False

        threading.Thread(target=worker, daemon=True).start()

    def _apply_connection(self, client: QBittorrentClient, settings: ConnectionSettings):
        self.client = client
        self.settings.connection = settings
        err = save_settings(self.settings)
        if err:
            self._show_error(f"Failed to save settings: {err}")
        if self._connect_menu_item:
            self._connect_menu_item.Enable(False)
        if self._disconnect_menu_item:
            self._disconnect_menu_item.Enable(True)
        self._connection_label.SetLabel("Connected")
        self._set_status("Connected")
        self._configure_timer()
        self.refresh_torrents()
        self._process_pending_items()

    def _disconnect(self):
        if self.client.is_authenticated:
            try:
                self.client.logout()
            except Exception:
                pass
        self._connection_label.SetLabel("Disconnected")
        self._list.DeleteAllItems()
        self._set_status("Disconnected")
        if self._connect_menu_item:
            self._connect_menu_item.Enable(True)
        if self._disconnect_menu_item:
            self._disconnect_menu_item.Enable(False)

    # ------------------------------------------------------------------

    def refresh_torrents(self, announce: bool = True):
        if announce:
            self._set_status("Refreshing torrents…")
        if not self.client.is_authenticated:
            if announce:
                self._set_status("Not connected")
            return
        selection = self._filter_choice.GetStringSelection()
        # If a tracker is selected, fetch "all" and filter client-side.
        api_filter = selection if selection in FILTER_CHOICES else "all"

        def worker():
            try:
                torrents = self.client.get_torrents(api_filter)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._show_error, f"Failed to refresh: {exc}")
            else:
                # Keep a copy for client-side filtering and tracker extraction.
                wx.CallAfter(self._after_torrents_loaded, torrents, announce)

        threading.Thread(target=worker, daemon=True).start()

    def _after_torrents_loaded(self, torrents: List[TorrentDetail], announce: bool):
        self._all_torrents = torrents
        # Update trackers list asynchronously; list will refresh when ready.
        self._ensure_tracker_hosts(torrents)
        # Apply current search/tracker filters to display list.
        self._apply_client_side_filters(announce)

    def _apply_client_side_filters(self, announce: bool = False):
        selection = self._filter_choice.GetStringSelection()
        search_text = self._search_ctrl.GetValue().strip().lower()
        # Start from the latest fetched torrents (already status-filtered if applicable)
        candidate = list(self._all_torrents)
        if selection not in FILTER_CHOICES and selection:
            # Tracker filter selected; narrow by tracker host.
            host = selection
            candidate = [t for t in candidate if host in self._tracker_map.get(t.infohash_v1, set())]
        if search_text:
            candidate = [t for t in candidate if search_text in (t.name or "").lower()]
        # Avoid heavy UI rebuild if nothing changed.
        new_hashes = [t.infohash_v1 for t in candidate]
        if new_hashes == self._displayed_hashes:
            if announce:
                self._set_status(f"Loaded {len(candidate)} torrents")
            return
        self._displayed_hashes = new_hashes
        self._list.update_from(candidate)
        if announce:
            self._set_status(f"Loaded {len(candidate)} torrents")

    # ----------------------- Trackers support -------------------------
    def _ensure_tracker_hosts(self, torrents: List[TorrentDetail]):
        # Determine which hashes need tracker lookup
        missing_hashes = [t.infohash_v1 for t in torrents if t.infohash_v1 not in self._tracker_map]
        if not missing_hashes:
            # Nothing to fetch; leave combo as-is to avoid heavy recompute.
            return

        # Process only a limited number per refresh to avoid spikes.
        to_fetch = missing_hashes[:MAX_TRACKER_LOOKUPS_PER_REFRESH]

        def worker():
            added_hosts: set = set()
            for h in to_fetch:
                try:
                    trackers = self.client.get_torrent_trackers(h)
                except Exception:
                    continue
                hosts = set()
                for tr in trackers:
                    url = (tr or {}).get("url", "")
                    try:
                        parsed = urlparse(url)
                    except Exception:
                        parsed = None
                    if not parsed or not parsed.scheme:
                        continue
                    host = parsed.hostname or parsed.netloc or ""
                    host = host.strip().lower()
                    if host:
                        hosts.add(host)
                if hosts:
                    self._tracker_map[h] = hosts
                    added_hosts.update(hosts)
                else:
                    self._tracker_map[h] = set()
            if added_hosts:
                wx.CallAfter(self._update_filter_combo_with_trackers, added_hosts)

        threading.Thread(target=worker, daemon=True).start()

    def _update_filter_combo_with_trackers(self, added_hosts: Optional[set] = None):
        # Incrementally update tracker set to avoid expensive full recompute.
        added_hosts = set(added_hosts or [])
        if not (added_hosts - self._tracker_hosts_set):
            return
        self._tracker_hosts_set.update(added_hosts)
        self._tracker_hosts = sorted(self._tracker_hosts_set)
        current = self._filter_choice.GetStringSelection()
        # Combine fixed filters + dynamic tracker hosts
        choices = FILTER_CHOICES + self._tracker_hosts
        self._filter_choice.Clear()
        self._filter_choice.AppendItems(choices)
        # Try to preserve selection
        if current in choices:
            self._filter_choice.SetStringSelection(current)
        else:
            default_filter = self.settings.default_filter if self.settings.default_filter in FILTER_CHOICES else FILTER_CHOICES[0]
            self._filter_choice.SetStringSelection(default_filter)
        # Re-apply filters because choices may have changed
        self._apply_client_side_filters()

    def _pause_selected(self):
        self._perform_on_selection("pause", self.client.pause_torrents)

    def _resume_selected(self):
        self._perform_on_selection("resume", self.client.resume_torrents)

    def _recheck_selected(self):
        self._perform_on_selection("recheck", self.client.recheck_torrents)

    def _delete_selected(self, delete_files: bool):
        if not self.client.is_authenticated:
            self._announce_not_connected()
            return
        torrents = self._list.get_selected_hashes()
        if not torrents:
            self._announce_require_selection()
            return
        if self.settings.confirm_delete:
            confirm_text = "Delete the selected torrents?"
            if delete_files:
                confirm_text = "Delete the selected torrents and downloaded data?"
            if wx.MessageBox(confirm_text, APP_TITLE, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
                return

        def worker():
            try:
                self.client.delete_torrents(torrents, delete_files=delete_files)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._show_error, f"Delete failed: {exc}")
            else:
                wx.CallAfter(self.refresh_torrents)

        threading.Thread(target=worker, daemon=True).start()

    def _perform_on_selection(self, action_name: str, callback):
        if not self.client.is_authenticated:
            self._announce_not_connected()
            return
        torrents = self._list.get_selected_hashes()
        if not torrents:
            self._announce_require_selection()
            return

        def worker():
            try:
                callback(torrents)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._show_error, f"Failed to {action_name}: {exc}")
            else:
                wx.CallAfter(self._set_status, f"Action '{action_name}' submitted")
                wx.CallAfter(self.refresh_torrents)

        threading.Thread(target=worker, daemon=True).start()

    def _open_details(self):
        torrent = self._list.get_selected()
        if not torrent:
            self._announce_require_selection()
            return
        dlg = DetailsDialog(self, torrent, self.client)
        dlg.ShowModal()
        dlg.Destroy()

    def _show_add_dialog(self):
        if not self.client.is_authenticated:
            self._announce_not_connected()
            return
        try:
            categories = list(self.client.get_categories().keys())
        except Exception:
            categories = []
        dlg = AddTorrentDialog(self, categories)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self._add_torrent(dlg.url, "", dlg.category, dlg.save_path)
        finally:
            dlg.Destroy()

    def _add_torrent(self, url: str, file_path: str, category: str, save_path: str):
        if not url and not file_path:
            self._show_error("Provide a magnet link or torrent file")
            return

        def worker():
            try:
                if url:
                    self.client.add_torrent_by_url(url, category or None, save_path or None)
                else:
                    self.client.add_torrent_file(file_path, category or None, save_path or None)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._show_error, f"Failed to add torrent: {exc}")
            else:
                wx.CallAfter(self._set_status, "Torrent added")
                time.sleep(1.0)
                wx.CallAfter(self.refresh_torrents)

        threading.Thread(target=worker, daemon=True).start()

    def _show_about(self):
        wx.MessageBox(
            "qBittorrent remote client built with wxPython.\n"
            "Designed for NVDA screen reader compatibility.",
            APP_TITLE,
        )

    def _announce_not_connected(self):
        self._show_error("Connect to a qBittorrent server first")

    def _announce_require_selection(self):
        self._show_error("Select a torrent first")

    def _show_error(self, message: str):
        self._set_status(message)
        wx.MessageBox(message, APP_TITLE, wx.OK | wx.ICON_ERROR)

    def _set_status(self, message: str):
        self.GetStatusBar().SetStatusText(message)

    def Destroy(self):
        self._timer.Stop()
        if self.client.is_authenticated:
            try:
                self.client.logout()
            except Exception:
                pass
        if self._taskbar_icon is not None:
            try:
                self._taskbar_icon.Destroy()
            except Exception:  # noqa: BLE001
                pass
            self._taskbar_icon = None
        return super().Destroy()


def format_speed(value: int) -> str:
    if value <= 0:
        return "0 B/s"
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s"]
    idx = 0
    speed = float(value)
    while speed >= 1024 and idx < len(units) - 1:
        speed /= 1024
        idx += 1
    return f"{speed:.1f} {units[idx]}"


def format_eta(seconds: int) -> str:
    if seconds < 0:
        return "infinite"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 48:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def format_progress(progress: float) -> str:
    return f"{progress * 100:.1f}%"


class AccessibleApp(wx.App):
    def __init__(self, pending_items: Optional[List[str]] = None):
        self._pending_items = list(pending_items or [])
        super().__init__()

    def OnInit(self):  # noqa: N802
        frame = MainFrame(self._pending_items)
        frame.Show()
        self.SetTopWindow(frame)
        return True


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qBittorrent remote client")
    parser.add_argument(
        "items",
        nargs="*",
        help="Torrent files or magnet links to add on startup",
    )
    parser.add_argument(
        "--register-associations",
        action="store_true",
        help="Register .torrent and magnet handlers for this application",
    )
    parser.add_argument(
        "--unregister-associations",
        action="store_true",
        help="Remove .torrent and magnet handlers created by this application",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    target_path = Path(sys.argv[0]).resolve()
    if args.register_associations and args.unregister_associations:
        print("Choose either --register-associations or --unregister-associations, not both.")
        return 1
    if args.register_associations:
        success, message = register_associations(target_path, sys.executable)
        print(message or ("Registered" if success else "Failed to register associations"))
        return 0 if success else 1
    if args.unregister_associations:
        success, message = unregister_associations(target_path)
        print(message or ("Removed" if success else "Failed to remove associations"))
        return 0 if success else 1

    pending = [item for item in args.items if item]
    app = AccessibleApp(pending)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
