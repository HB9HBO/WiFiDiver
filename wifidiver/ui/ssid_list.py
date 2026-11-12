"""Eigene SSID-Liste: Netze markieren und wahlweise nur diese oder nur die anderen anzeigen."""

from __future__ import annotations

from fnmatch import fnmatchcase

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QPushButton, QVBoxLayout)

MODES = (("all", "Alle SSIDs"), ("only", "Nur Liste"), ("hide", "Liste ausblenden"))
MARK = "★ "


class SsidList:
    """SSIDs bzw. Muster mit * und ? (z.B. 'FRITZ!Box*'), gespeichert in den Einstellungen."""

    KEY = "ssid_list/entries"

    def __init__(self, settings: QSettings):
        self.settings = settings
        raw = settings.value(self.KEY, [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        self.entries: list[str] = [str(e) for e in (raw or []) if str(e)]
        self._cache: dict[str, bool] = {}

    def save(self) -> None:
        self.settings.setValue(self.KEY, list(self.entries))
        self._cache.clear()

    def set_entries(self, entries: list[str]) -> None:
        self.entries = list(dict.fromkeys(e for e in entries if e))
        self.save()

    def add(self, ssid: str) -> None:
        if ssid and ssid not in self.entries:
            self.entries.append(ssid)
            self.save()

    def remove(self, ssid: str) -> None:
        """Entfernt den exakten Eintrag (Muster bleiben bestehen)."""
        if ssid in self.entries:
            self.entries.remove(ssid)
            self.save()

    def matches(self, ssid: str) -> bool:
        if not ssid:
            return False
        hit = self._cache.get(ssid)
        if hit is None:
            hit = any(ssid == e or (("*" in e or "?" in e) and fnmatchcase(ssid, e))
                      for e in self.entries)
            self._cache[ssid] = hit
        return hit

    def accepts(self, ssid: str, mode: str) -> bool:
        if mode == "only":
            return self.matches(ssid)
        if mode == "hide":
            return not self.matches(ssid)
        return True


class SsidListDialog(QDialog):
    def __init__(self, entries: list[str], current_ssids: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("SSID-Liste")
        self.resize(420, 460)
        lay = QVBoxLayout(self)
        info = QLabel("Eigene bzw. bekannte Netze. Über den Filter „Nur Liste“ oder „Liste ausblenden“ "
                      "werden Tabelle, Kanalgraph und Signalverlauf eingeschränkt.\n"
                      "Muster mit * und ? sind möglich, z.B. „FRITZ!Box*“.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.setSortingEnabled(True)
        for e in entries:
            self._add_item(e)
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("SSID oder Muster eingeben …")
        row.addWidget(self.edit, 1)
        add = QPushButton("Hinzufügen")
        row.addWidget(add)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        rem = QPushButton("Entfernen")
        row2.addWidget(rem)
        pick = QPushButton("Aus aktuellem Scan …")
        row2.addWidget(pick)
        row2.addStretch()
        lay.addLayout(row2)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        lay.addWidget(bb)

        self.current = sorted({s for s in current_ssids if s}, key=str.lower)
        add.clicked.connect(self._add_text)
        self.edit.returnPressed.connect(self._add_text)
        rem.clicked.connect(self._remove)
        pick.clicked.connect(self._pick)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

    def _add_item(self, text: str) -> None:
        if text and not self.list.findItems(text, Qt.MatchFlag.MatchExactly):
            self.list.addItem(QListWidgetItem(text))

    def _add_text(self) -> None:
        self._add_item(self.edit.text().strip())
        self.edit.clear()

    def _remove(self) -> None:
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def _pick(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("SSIDs aus dem Scan übernehmen")
        dlg.resize(360, 420)
        v = QVBoxLayout(dlg)
        lst = QListWidget()
        for s in self.current:
            it = QListWidgetItem(s)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if self.list.findItems(s, Qt.MatchFlag.MatchExactly)
                             else Qt.CheckState.Unchecked)
            lst.addItem(it)
        v.addWidget(lst)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        v.addWidget(bb)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        if dlg.exec():
            for i in range(lst.count()):
                it = lst.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    self._add_item(it.text())
                else:
                    for old in self.list.findItems(it.text(), Qt.MatchFlag.MatchExactly):
                        self.list.takeItem(self.list.row(old))

    def entries(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]
