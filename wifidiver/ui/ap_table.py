"""Tabellenmodell für Access Points."""

from __future__ import annotations

import hashlib

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor, QFont

from ..wifi.channels import DFS_CHANNELS_5G
from ..wifi.model import AccessPoint


def ap_color(bssid: str) -> QColor:
    h = int(hashlib.md5(bssid.encode()).hexdigest()[:6], 16)
    return QColor.fromHsv(h % 360, 170 + (h >> 9) % 60, 230)


def signal_color(dbm: float) -> QColor:
    if dbm >= -60:
        return QColor(80, 200, 120)
    if dbm >= -70:
        return QColor(200, 200, 80)
    if dbm >= -80:
        return QColor(230, 150, 60)
    return QColor(220, 80, 80)


COLUMNS = [
    ("", None),
    ("SSID", "display_ssid"),
    ("BSSID", "bssid"),
    ("Hersteller", "vendor"),
    ("Band", "band"),
    ("Kanal", "channel"),
    ("Breite", "width"),
    ("Mitte (MHz)", "center_freq"),
    ("Signal (dBm)", "signal"),
    ("Sicherheit", "security"),
    ("Generation", "generation"),
    ("Auslastung", "utilization"),
    ("Clients", "stations"),
    ("Zuletzt", "last_seen_ms"),
]


class ApTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.aps: list[AccessPoint] = []
        self._bold = QFont()
        self._bold.setBold(True)

    def set_aps(self, aps: list[AccessPoint]) -> None:
        old = {ap.bssid: i for i, ap in enumerate(self.aps)}
        new_keys = [ap.bssid for ap in aps]
        if [a.bssid for a in self.aps] == new_keys:
            self.aps = list(aps)
            self.dataChanged.emit(self.index(0, 0), self.index(len(aps) - 1, len(COLUMNS) - 1))
            return
        # stabile Reihenfolge: bekannte zuerst in alter Ordnung, neue hinten
        by_key = {ap.bssid: ap for ap in aps}
        ordered = [by_key[k] for k in sorted(by_key, key=lambda k: old.get(k, 1 << 30))]
        self.beginResetModel()
        self.aps = ordered
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.aps)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][0]
        return None

    def _display(self, ap: AccessPoint, attr: str):
        v = getattr(ap, attr)
        if attr == "band":
            return f"{v} GHz" if v else ""
        if attr == "width":
            return f"{v} MHz"
        if attr == "center_freq":
            return f"{v:.0f}"
        if attr == "signal":
            return f"{v:.0f}"
        if attr == "utilization":
            return "" if v is None else f"{v:.0f} %"
        if attr == "stations":
            return "" if v is None else str(v)
        if attr == "last_seen_ms":
            return "" if v is None else (f"{v} ms" if v < 1000 else f"{v / 1000:.1f} s")
        if attr == "channel":
            dfs = ap.band == "5" and v in DFS_CHANNELS_5G
            return f"{v} (DFS)" if dfs else str(v)
        return str(v)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ap = self.aps[index.row()]
        col = index.column()
        attr = COLUMNS[col][1]
        if role == Qt.ItemDataRole.DisplayRole and attr:
            return self._display(ap, attr)
        if role == Qt.ItemDataRole.UserRole:  # Sortierschlüssel
            if attr is None:
                return 0
            v = getattr(ap, attr)
            return -1e9 if v is None else v
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return ap_color(ap.bssid)
        if role == Qt.ItemDataRole.ForegroundRole and attr == "signal":
            return QBrush(signal_color(ap.signal))
        if role == Qt.ItemDataRole.FontRole and ap.connected:
            return self._bold
        if role == Qt.ItemDataRole.ToolTipRole and ap.connected:
            return "verbunden"
        if role == Qt.ItemDataRole.TextAlignmentRole and attr in (
                "channel", "width", "center_freq", "signal", "utilization", "stations", "last_seen_ms"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class ApFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.band = None
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_text(self, text: str) -> None:
        self.text = text.lower().strip()
        self.invalidateFilter()

    def set_band(self, band: str | None) -> None:
        self.band = band
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        ap = self.sourceModel().aps[row]
        if self.band and ap.band != self.band:
            return False
        if not self.text:
            return True
        hay = " ".join(str(x) for x in (ap.ssid, ap.bssid, ap.vendor, ap.security,
                                         ap.generation, ap.channel)).lower()
        return all(t in hay for t in self.text.split())

    def lessThan(self, left, right):
        a = left.data(Qt.ItemDataRole.UserRole)
        b = right.data(Qt.ItemDataRole.UserRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)
