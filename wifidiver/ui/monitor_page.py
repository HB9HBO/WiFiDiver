"""Tab „Monitor“: Monitormodus steuern, Kanalauslastung, Clients, Netze und Ereignisse anzeigen."""

from __future__ import annotations

import time

from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                             QProgressBar, QPushButton, QSpinBox, QSplitter, QTableWidget,
                             QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ..wifi import oui, scanners
from ..wifi.channels import band_of_freq, freq_to_channel
from ..wifi.ieee80211 import is_unicast
from ..wifi.monitor import MonitorBackend, allowed_freqs, helper_status

BANDS = (("2.4", "2,4 GHz"), ("5", "5 GHz"), ("6", "6 GHz"))


class _Item(QTableWidgetItem):
    """Tabellenzelle, die nach einem Zahlenwert sortiert (sort_key)."""

    def __init__(self, text: str, key=None):
        super().__init__(text)
        self.key = text.lower() if key is None else key

    def __lt__(self, other):
        try:
            return self.key < other.key
        except TypeError:
            return str(self.key) < str(other.key)


def _ago(t: float, now: float) -> str:
    d = max(0.0, now - t)
    return f"{d:.0f} s" if d < 90 else f"{d / 60:.0f} min"


def _busy_color(pct: float) -> QColor:
    """grün -> gelb -> rot, halbtransparent als Hintergrund."""
    p = max(0.0, min(1.0, pct / 70.0))
    r = int(60 + 190 * min(1.0, p * 2))
    g = int(200 - 150 * max(0.0, p * 2 - 1))
    return QColor(r, g, 60, 90)


class MonitorPage(QWidget):
    updated = pyqtSignal()                 # neuer Schnappschuss
    runningChanged = pyqtSignal(bool)
    alert = pyqtSignal(int)                # Anzahl auffälliger Ereignisse der letzten 5 Minuten

    CH_COLS = ("Kanal", "MHz", "Belegung", "Frames/s", "Wiederholungen", "Netze", "Clients", "beobachtet",
               "Frames gesamt")
    STA_COLS = ("Client (MAC)", "Hersteller", "Verbunden mit", "Pegel", "Frames", "Daten",
                "Wiederh.", "Kanal", "seit", "zuletzt", "Gesuchte Netze")
    NET_COLS = ("SSID", "BSSID", "Hersteller", "Kanal", "Pegel", "Sicherheit", "PMF", "Beacons",
                "Daten-Frames", "Wiederh.", "Clients", "Deauth/Disassoc", "seit", "zuletzt")
    EV_COLS = ("Zeit", "Bewertung", "Art", "Netz", "Absender (angeblich)", "Ziel", "Anzahl",
               "Grund", "Pegel", "Hinweise", "Möglicher Sender")

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.backend = MonitorBackend()
        self.names: dict[str, str] = {}             # BSSID -> SSID (aus Scan und Monitor)
        self._freqs: list[int] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        row = QHBoxLayout()
        row.addWidget(QLabel("Interface:"))
        self.iface = QComboBox()
        self.iface.setToolTip("WLAN-Adapter für den Monitormodus. Er sollte nicht von NetworkManager "
                              "verwaltet werden (siehe README).")
        row.addWidget(self.iface)
        self.band_cb: dict[str, QCheckBox] = {}
        for key, name in BANDS:
            cb = QCheckBox(name)
            cb.setChecked(str(settings.value(f"monitor/band_{key}", key != "6")) in ("true", "True"))
            cb.toggled.connect(self._bands_changed)
            self.band_cb[key] = cb
            row.addWidget(cb)
        row.addWidget(QLabel("  Kanal:"))
        self.channel = QComboBox()
        self.channel.setToolTip("„alle“: Kanäle reihum abhören (Überblick)\n"
                                "fester Kanal: dauerhaft zuhören (genaue Auslastung, feiner Signalverlauf)")
        row.addWidget(self.channel)
        row.addWidget(QLabel("  Verweilzeit:"))
        self.dwell = QSpinBox()
        self.dwell.setRange(50, 2000)
        self.dwell.setSingleStep(50)
        self.dwell.setSuffix(" ms")
        self.dwell.setValue(int(settings.value("monitor/dwell", 250)))
        self.dwell.setToolTip("So lange bleibt der Adapter beim Kanalwechsel auf jedem Kanal")
        row.addWidget(self.dwell)
        self.start_btn = QPushButton("Start")
        self.start_btn.setCheckable(True)
        row.addWidget(self.start_btn)
        row.addStretch()
        lay.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.status)

        split = QSplitter(Qt.Orientation.Vertical)
        self.ch_table = self._table(self.CH_COLS)
        self.sta_table = self._table(self.STA_COLS)
        self.sta_table.horizontalHeader().setSectionResizeMode(len(self.STA_COLS) - 1,
                                                               QHeaderView.ResizeMode.Stretch)
        box1 = QWidget()
        v1 = QVBoxLayout(box1)
        v1.setContentsMargins(0, 0, 0, 0)
        self.ch_title = QLabel("<b>Kanalauslastung</b>")
        v1.addWidget(self.ch_title)
        v1.addWidget(self.ch_table)
        box2 = QWidget()
        v2 = QVBoxLayout(box2)
        v2.setContentsMargins(0, 0, 0, 0)
        r2 = QHBoxLayout()
        self.sta_title = QLabel("")
        r2.addWidget(self.sta_title)
        r2.addStretch()
        self.only_conn = QCheckBox("nur verbundene Clients")
        self.only_conn.setChecked(str(settings.value("monitor/only_connected", False)) in ("true", "True"))
        self.only_conn.toggled.connect(lambda _: self._show())
        r2.addWidget(self.only_conn)
        v2.addLayout(r2)
        v2.addWidget(self.sta_table)

        self.net_table = self._table(self.NET_COLS)

        box3 = QWidget()
        v3 = QVBoxLayout(box3)
        v3.setContentsMargins(0, 0, 0, 0)
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Ortung:"))
        self.meter = QProgressBar()
        self.meter.setRange(0, 80)                  # -100 … -20 dBm
        self.meter.setTextVisible(False)
        self.meter.setMaximumWidth(260)
        self.meter.setToolTip("Pegel des zuletzt empfangenen Deauth/Disassoc-Frames. Zum Aufspüren des "
                              "Senders einen festen Kanal wählen und mit dem Adapter (am besten mit "
                              "Richtantenne) dem steigenden Pegel folgen.")
        r3.addWidget(self.meter)
        self.meter_lbl = QLabel("")
        r3.addWidget(self.meter_lbl, 1)
        self.hold_btn = QPushButton("Auf diesem Kanal bleiben")
        self.hold_btn.setToolTip("Kanalwechsel beenden und auf dem Kanal des ausgewählten (sonst des "
                                 "letzten) Ereignisses zuhören")
        self.hold_btn.clicked.connect(self._hold_channel)
        r3.addWidget(self.hold_btn)
        v3.addLayout(r3)
        self.ev_table = self._table(self.EV_COLS)
        self.ev_table.horizontalHeader().setSectionResizeMode(len(self.EV_COLS) - 2,
                                                              QHeaderView.ResizeMode.Stretch)
        self.ev_table.doubleClicked.connect(lambda _: self._hold_channel())
        v3.addWidget(self.ev_table)
        note = QLabel("<span style='color:#9a9a9a'>Rein passive Erkennung. Gefälschte Frames tragen die "
                      "Adresse des Opfers – „Möglicher Sender“ sind nur Geräte, die zur selben Zeit mit "
                      "ähnlichem Pegel gehört wurden. Beim Kanalwechsel wird jeder Kanal nur kurz "
                      "abgehört; zur Überwachung einen festen Kanal wählen.</span>")
        note.setWordWrap(True)
        v3.addWidget(note)

        self.sub = QTabWidget()
        self.sub.addTab(box2, "Clients")
        self.sub.addTab(self.net_table, "Netze")
        self.sub.addTab(box3, "Ereignisse")
        self.sub.currentChanged.connect(lambda _: self._show())
        self._events: list[dict] = []
        split.addWidget(box1)
        split.addWidget(self.sub)
        split.setSizes([300, 400])
        lay.addWidget(split, 1)

        self.refresh_interfaces()
        self.iface.currentTextChanged.connect(lambda _: self._bands_changed())
        self._bands_changed()
        saved_ch = settings.value("monitor/channel", 0)
        i = self.channel.findData(int(saved_ch or 0))
        self.channel.setCurrentIndex(max(i, 0))
        self.start_btn.toggled.connect(self._toggle)
        self.channel.currentIndexChanged.connect(lambda _: self._retune())
        self.dwell.valueChanged.connect(lambda _: self._retune())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(500)
        self._update_status()

    # ------------------------------------------------------------------ Aufbau
    @staticmethod
    def _table(cols) -> QTableWidget:
        t = QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.setSortingEnabled(True)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        t.verticalHeader().setDefaultSectionSize(22)
        return t

    def refresh_interfaces(self) -> None:
        cur = self.iface.currentText() or str(self.settings.value("monitor/iface", ""))
        self.iface.blockSignals(True)
        self.iface.clear()
        self.iface.addItems(scanners.wireless_interfaces() or [])
        if cur and self.iface.findText(cur) < 0 and cur:
            self.iface.addItem(cur)
        if cur:
            self.iface.setCurrentText(cur)
        self.iface.blockSignals(False)

    def _bands_changed(self) -> None:
        iface = self.iface.currentText()
        allowed = allowed_freqs(iface) if iface else []
        bands = {k for k, cb in self.band_cb.items() if cb.isChecked()}
        avail = {band_of_freq(f) for f in allowed}
        for k, cb in self.band_cb.items():
            cb.setEnabled(not allowed or k in avail)
            cb.setToolTip("" if not allowed or k in avail else
                          "Keine freigegebenen Kanäle (Adapter oder Regulierungsdomäne, siehe README)")
        self._freqs = [f for f in allowed if band_of_freq(f) in bands]
        cur = self.channel.currentData()
        self.channel.blockSignals(True)
        self.channel.clear()
        self.channel.addItem(f"alle ({len(self._freqs)} Kanäle)", 0)
        for f in self._freqs:
            self.channel.addItem(f"{freq_to_channel(f)}  ({f} MHz)", f)
        i = self.channel.findData(cur)
        self.channel.setCurrentIndex(max(i, 0))
        self.channel.blockSignals(False)
        self._retune()

    def hop_list(self) -> list[int]:
        f = self.channel.currentData()
        return [f] if f else list(self._freqs)

    # ------------------------------------------------------------------ Steuerung
    def _toggle(self, on: bool) -> None:
        if on:
            iface = self.iface.currentText()
            if not iface:
                self.backend.error = "kein WLAN-Interface gefunden"
                ok = False
            else:
                ok = self.backend.start(iface, self.hop_list(), self.dwell.value())
            if not ok:
                self.start_btn.blockSignals(True)
                self.start_btn.setChecked(False)
                self.start_btn.blockSignals(False)
        else:
            self.backend.stop()
        self.start_btn.setText("Stop" if self.backend.running else "Start")
        self.iface.setEnabled(not self.backend.running)
        self.runningChanged.emit(self.backend.running)
        self._update_status()

    def _retune(self) -> None:
        if self.backend.running:
            self.backend.set_hop(self.hop_list(), self.dwell.value())
        self._save()

    def stop(self) -> None:
        self.backend.stop()

    def _save(self) -> None:
        s = self.settings
        s.setValue("monitor/iface", self.iface.currentText())
        for k, cb in self.band_cb.items():
            s.setValue(f"monitor/band_{k}", cb.isChecked())
        s.setValue("monitor/channel", self.channel.currentData() or 0)
        s.setValue("monitor/dwell", self.dwell.value())
        s.setValue("monitor/only_connected", self.only_conn.isChecked())

    # ------------------------------------------------------------------ Anzeige
    def _poll(self) -> None:
        was = self.backend.proc is not None
        if not self.backend.poll():
            return
        if was and not self.backend.running:
            self.start_btn.blockSignals(True)
            self.start_btn.setChecked(False)
            self.start_btn.setText("Start")
            self.start_btn.blockSignals(False)
            self.iface.setEnabled(True)
            self.runningChanged.emit(False)
        self._update_status()
        snap = self.backend.snapshot
        if snap is not None:
            self._events = snap.get("events", [])
            now = time.time()
            n = sum(1 for e in self._events if e["severity"] >= 1 and now - e["last"] < 300)
            self.sub.setTabText(2, f"Ereignisse ({n} auffällig)" if n else
                                f"Ereignisse ({len(self._events)})")
            self.alert.emit(n)
        if self.isVisible():
            self._show()
        self.updated.emit()

    def _update_status(self) -> None:
        b = self.backend
        snap = b.snapshot
        if not b.running:
            _, err = helper_status()
            text = b.error or err or "gestoppt"
            col = "#e0a040" if (b.error or err) and b.error != "gestoppt" else "#9a9a9a"
            self.status.setStyleSheet(f"color: {col};")
            self.status.setText(text)
            return
        self.status.setStyleSheet("color: #9a9a9a;")
        if snap is None or snap.get("ready") is None:
            self.status.setText("starte …")
            return
        name, mode = snap["ready"]
        parts = [f"{name} ({mode})"]
        hopping = len(self.hop_list()) > 1
        if hopping and self.dwell.value() < 1000:
            # Die Anzeige wird nur alle 500 ms erneuert – bei kürzerer Verweilzeit sähe der
            # gerade gezeigte Kanal so aus, als würden Kanäle übersprungen.
            parts.append(f"wechselt über {len(self.hop_list())} Kanäle, je {self.dwell.value()} ms")
        elif snap["freq"]:
            parts.append(f"Kanal {freq_to_channel(snap['freq'])} ({snap['freq']} MHz)")
        if hopping and snap["cycle_s"]:
            parts.append(f"Durchlauf {snap['cycle_s']:.1f} s")
        parts.append(f"{snap['frames']} Frames, {snap.get('beacons', 0)} Beacons")
        if snap.get("bad"):
            parts.append(f"{snap['bad']} fehlerhaft (FCS)")
        if snap["drops"]:
            parts.append(f"{snap['drops']} verworfen (CPU zu langsam?)")
        if snap["messages"]:
            parts.append(snap["messages"][-1])
        if snap["frames"] >= 30 and snap.get("beacons", 0) == 0:
            # bekannter Fehler: mt7921 mit Kernel 6.17/6.18 filtert im Monitormodus die Beacons
            self.status.setStyleSheet("color: #e0a040;")
            parts.append("Es kommen Frames, aber keine Beacons an – Treiberproblem? "
                         "mt7921 (z.B. AWUS036AXML) braucht Kernel ≤ 6.16 oder ≥ 6.19, siehe README")
        self.status.setText("   ·   ".join(parts))

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._show()

    def _show(self) -> None:
        snap = self.backend.snapshot
        if snap is None:
            return
        now = time.time()
        bss, sta = snap["bss"], snap["sta"]
        for k, d in bss.items():
            if d["ssid"]:
                self.names[k] = d["ssid"]
        # Kanäle
        nets: dict[int, int] = {}
        for d in bss.values():
            if d["info"]:
                f = int(d["info"]["freq"])
                nets[f] = nets.get(f, 0) + 1
        clients: dict[int, int] = {}
        for d in sta.values():
            if d["bssid"] and d["freq"]:
                clients[d["freq"]] = clients.get(d["freq"], 0) + 1
        rows = []
        for f, c in sorted(snap["chan"].items()):
            busy = c["busy"] or 0.0
            rows.append([
                _Item(str(freq_to_channel(f)), freq_to_channel(f) + (0 if f < 5900 else 1000)),
                _Item(str(f), f),
                _Item(f"{busy:.0f} %", busy),
                _Item(f"{c['fps']:.0f}", c["fps"]),
                _Item(f"{c['retry']:.0f} %", c["retry"]),
                _Item(str(nets.get(f, 0)), nets.get(f, 0)),
                _Item(str(clients.get(f, 0)), clients.get(f, 0)),
                _Item(f"{c['observed']:.0f} s", c["observed"]),
                _Item(str(c.get("frames", 0)), c.get("frames", 0)),
            ])
            rows[-1][2].setBackground(QBrush(_busy_color(busy)))
            if f == snap["freq"] and (len(self.hop_list()) <= 1 or self.dwell.value() >= 1000):
                for it in rows[-1]:
                    it.setForeground(QBrush(QColor(120, 200, 255)))
        self._fill(self.ch_table, rows)
        self.ch_title.setText("<b>Kanalauslastung</b> &nbsp; <span style='color:#9a9a9a'>"
                              "geschätzt aus den empfangenen Frames, nur der 20-MHz-Kanal, auf dem "
                              "der Adapter gerade hört</span>")
        # Clients
        rows = []
        only = self.only_conn.isChecked()
        n_conn = 0
        for m, d in sta.items():
            if d["bssid"]:
                n_conn += 1
            elif only:
                continue
            ap = d["bssid"]
            ap_txt = f"{self.names.get(ap, '?')}  ({ap})" if ap else "– (sucht)"
            sig = d["signal"]
            vendor = "zufällige Adresse" if d["random"] else (oui.lookup(m) or "")
            retry = d["retries"] / d["frames"] * 100 if d["frames"] else 0.0
            rows.append([
                _Item(m), _Item(vendor), _Item(ap_txt),
                _Item("–" if sig is None else f"{sig:.0f} dBm", -999 if sig is None else sig),
                _Item(str(d["frames"]), d["frames"]),
                _Item(_bytes(d["bytes"]), d["bytes"]),
                _Item(f"{retry:.0f} %", retry),
                _Item(str(freq_to_channel(d["freq"])) if d["freq"] else "–", d["freq"]),
                _Item(_ago(d["first"], now), -d["first"]),
                _Item(_ago(d["last"], now), -d["last"]),
                _Item(", ".join(d["probes"])),
            ])
        self._fill(self.sta_table, rows)
        self.sta_title.setText(f"{n_conn} verbunden, {len(sta) - n_conn} suchend")
        self.sub.setTabText(0, f"Clients ({len(sta)})")
        self.sub.setTabText(1, f"Netze ({sum(1 for d in bss.values() if d['info'])})")
        cur = self.sub.currentIndex()
        if cur == 1:
            self._show_nets(bss, now)
        elif cur == 2:
            self._show_events(snap, now)

    def _ssid(self, bssid: str) -> str:
        return self.names.get(bssid, "")

    def _show_nets(self, bss: dict, now: float) -> None:
        rows = []
        for k, d in bss.items():
            info = d["info"]
            if info is None and not d["deauth"]:
                continue
            sig = d["signal"]
            name = d["ssid"] or self._ssid(k)
            ssid = name + (" (versteckt)" if d["hidden"] else "") if name else (
                "(versteckt)" if d["hidden"] else "")
            f = int(info["freq"]) if info else 0
            pmf = {"required": "erforderlich", "capable": "optional"}.get(info.get("pmf", ""), "nein") \
                if info else ""
            retry = d["retries"] / d["frames"] * 100 if d["frames"] else 0.0
            rows.append([
                _Item(ssid), _Item(k), _Item(oui.lookup(k) or ""),
                _Item(str(freq_to_channel(f)) if f else "–", f),
                _Item("–" if sig is None else f"{sig:.0f} dBm", -999 if sig is None else sig),
                _Item(info["security"] if info else ""), _Item(pmf),
                _Item(str(d["beacons"]), d["beacons"]), _Item(str(d["frames"]), d["frames"]),
                _Item(f"{retry:.0f} %", retry), _Item(str(d["clients"]), d["clients"]),
                _Item(str(d["deauth"]), d["deauth"]),
                _Item(_ago(d["first"], now), -d["first"]), _Item(_ago(d["last"], now), -d["last"]),
            ])
            if d["deauth"]:
                rows[-1][11].setBackground(QBrush(QColor(230, 160, 60, 110)))
        self._fill(self.net_table, rows)

    def _show_events(self, snap: dict, now: float) -> None:
        rows = []
        colors = {2: QColor(230, 70, 60, 110), 1: QColor(230, 160, 60, 90)}
        for e in self._events:
            net = e["bssid"]
            name = self._ssid(net)
            net_txt = f"{name}  ({net})" if name else net
            tg = e["targets"]
            tg_txt = ", ".join("alle" if not is_unicast(t) else (self._ssid(t) or t) for t in tg[:3])
            if e["n_targets"] > 3:
                tg_txt += f" … ({e['n_targets']})"
            sig = e["signal"]
            sig_txt = "–" if sig is None else f"{sig:.0f} dBm"
            if sig is not None and e["ref"] is not None:
                sig_txt += f" (Absender {e['ref']:.0f})"
            sus = ", ".join(f"{m} {oui.lookup(m) or ''} {s:.0f} dBm".replace("  ", " ")
                            for m, s in e["suspects"])
            rows.append([
                _Item(time.strftime("%H:%M:%S", time.localtime(e["last"])), -e["last"]),
                _Item(e["verdict"], -e["severity"]), _Item(e["kind"]), _Item(net_txt),
                _Item(e["src"]), _Item(tg_txt),
                _Item(str(e["count"]), e["count"]), _Item(", ".join(e["reasons"])),
                _Item(sig_txt, -999 if sig is None else sig),
                _Item("; ".join(h for h in e["hints"] if h)), _Item(sus),
            ])
            rows[-1][0].setData(Qt.ItemDataRole.UserRole, e["freq"])
            col = colors.get(e["severity"])
            if col is not None:
                rows[-1][1].setBackground(QBrush(col))
        self._fill(self.ev_table, rows)
        hit = snap.get("threat_last")
        if hit and time.time() - hit[1] < 10:
            sig, t, f, bssid = hit
            self.meter.setValue(int(max(0, min(80, sig + 100))))
            name = self._ssid(bssid)
            self.meter_lbl.setText(f"<b>{sig:.0f} dBm</b>  vor {time.time() - t:.0f} s, Kanal "
                                   f"{freq_to_channel(f)}, Netz {name or bssid}")
        else:
            self.meter.setValue(0)
            self.meter_lbl.setText("<span style='color:#9a9a9a'>kein Deauth/Disassoc in den letzten "
                                   "10 s</span>")

    def _hold_channel(self) -> None:
        f = 0
        rows = self.ev_table.selectionModel().selectedRows() if self.ev_table.selectionModel() else []
        if rows:
            it = self.ev_table.item(rows[0].row(), 0)
            f = it.data(Qt.ItemDataRole.UserRole) if it is not None else 0
        if not f:
            hit = (self.backend.snapshot or {}).get("threat_last")
            f = hit[2] if hit else 0
        if not f:
            return
        i = self.channel.findData(int(f))
        if i >= 0:
            self.channel.setCurrentIndex(i)

    @staticmethod
    def _fill(table: QTableWidget, rows: list) -> None:
        sort_col = table.horizontalHeader().sortIndicatorSection()
        order = table.horizontalHeader().sortIndicatorOrder()
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for r, items in enumerate(rows):
            for c, it in enumerate(items):
                table.setItem(r, c, it)
        table.setSortingEnabled(True)
        table.sortItems(sort_col, order)
        if table.property("sized") is None and rows:
            table.resizeColumnsToContents()
            table.setProperty("sized", True)

    # ------------------------------------------------------------------ für das Hauptfenster
    def set_names(self, aps) -> None:
        for ap in aps:
            if ap.ssid:
                self.names[ap.bssid] = ap.ssid


def _bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.0f} TB"
