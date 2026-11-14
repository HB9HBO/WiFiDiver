"""Hauptfenster: AP-Tabelle, Bandgraphen mit SDR-Wasserfall, Signalverlauf."""

from __future__ import annotations

import time

from PyQt6.QtCore import QSettings, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton,
                             QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableView, QTabWidget, QVBoxLayout,
                             QWidget)

from ..sdr import BACKENDS
from ..sdr.base import SdrBackend
from ..sdr.demo import DemoBackend
from ..wifi import overlays, scanners
from ..wifi.connection import ConnectionView
from ..wifi.details import ap_details, raw_text
from ..wifi.model import AccessPoint
from .ap_table import ApFilterProxy, ApTableModel
from .band_panel import BandPanel
from .history import HistoryWidget
from .info_page import InfoPage
from .monitor_page import MonitorPage
from .ssid_list import MODES, SsidList, SsidListDialog
from .sdr_panel import SdrPanel


class ScanWorker(QThread):
    scanned = pyqtSignal(list)
    failed = pyqtSignal(str)
    link = pyqtSignal(object)          # LinkInfo | None (aktuelle Verbindung)

    def __init__(self, scanner: scanners.Scanner, interval: float):
        super().__init__()
        self.scanner = scanner
        self.interval = interval
        self.paused = False
        self._quit = False

    def run(self) -> None:
        while not self._quit:
            t0 = time.time()
            if not self.paused:
                try:
                    self.scanned.emit(self.scanner.scan())
                except Exception as exc:  # noqa: BLE001
                    self.failed.emit(f"{self.scanner.name}: {exc}")
            try:
                self.link.emit(self.scanner.connection())
            except Exception:  # noqa: BLE001 - Verbindungsanzeige ist optional
                self.link.emit(None)
            while not self._quit and time.time() - t0 < self.interval:
                self.msleep(100)

    def stop(self) -> None:
        self._quit = True
        self.wait(15000)


def _coerce(param, value):
    if value is None:
        return param.default
    try:
        if param.kind == "bool":
            return value in (True, "true", "True", 1, "1")
        if param.kind == "int":
            return int(float(value))
        if param.kind == "float":
            return float(value)
        if param.kind == "choice":
            for c in param.choices:
                if str(c) == str(value):
                    return c
            return param.default
    except (TypeError, ValueError):
        return param.default
    return str(value)


class MainWindow(QMainWindow):
    BAND_KEYS = ["2.4", "5", "6"]

    def __init__(self, source: str = "auto", iface: str | None = None,
                 sdr: str | None = None, autostart: bool = False, band: str | None = None):
        super().__init__()
        self.setWindowTitle("WifiDiver – WLAN-Analyse mit SDR-Wasserfall")
        self.resize(1500, 950)
        self.settings = QSettings("wifidiver", "wifidiver")
        self.aps: list[AccessPoint] = []
        self.selected: str | None = None
        self.backend: SdrBackend | None = None
        self.sdr_band: str = band or str(self.settings.value("band", "2.4"))
        self._sweep_times: list[float] = []
        self._last_sweep = None
        self.ssid_list = SsidList(self.settings)
        self.conn_view = ConnectionView()
        self._link = None

        self._build_toolbar(source, iface)
        self._build_central()
        self._build_sdr_dock()

        # Scan-Thread
        self.worker = ScanWorker(self._make_scanner(), float(self.interval.value()))
        self.worker.scanned.connect(self._on_scan)
        self.worker.failed.connect(self._on_scan_failed)
        self.worker.link.connect(self._on_link)
        self.worker.start()

        # GUI-Timer für SDR-Daten
        self._frame_dt = 0.04
        self._status_t = 0.0
        self._auto_lv_until = 0.0           # Pegel automatisch anpassen, sobald genug Sweeps da sind
        self._frame_t = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_sdr)
        self.timer.start(40)

        self.status_aps = QLabel("")
        self.status_sdr = QLabel("SDR aus")
        self.statusBar().addWidget(self.status_aps, 1)
        self.statusBar().addPermanentWidget(self.status_sdr)

        if self.sdr_band in self.BAND_KEYS:
            self.tabs.setCurrentIndex(self.BAND_KEYS.index(self.sdr_band))
        self._sync_range_ui()
        sdr_name = sdr or str(self.settings.value("sdr/backend", "HackRF"))
        if sdr_name not in BACKENDS:
            sdr_name = "HackRF"
        combo = self.sdr_panel.backend_combo
        combo.blockSignals(True)
        combo.setCurrentText(sdr_name)
        combo.blockSignals(False)
        self.sdr_panel._backend_changed(sdr_name)
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if autostart:
            QTimer.singleShot(300, lambda: self._on_run_toggled(True))

    # ------------------------------------------------------------------ Aufbau
    def _build_toolbar(self, source: str, iface: str | None) -> None:
        tb = self.addToolBar("WLAN")
        self.toolbar = tb
        tb.setMovable(False)
        tb.addWidget(QLabel(" WLAN-Quelle: "))
        self.source = QComboBox()
        self.source.addItems(["auto", "iw", "nmcli", "monitor", "demo"])
        self.source.setItemData(3, "Netze aus dem Monitormodus (Tab „Monitor“)", Qt.ItemDataRole.ToolTipRole)
        self.source.setCurrentText(source)
        tb.addWidget(self.source)
        tb.addWidget(QLabel("  Interface: "))
        self.iface = QComboBox()
        ifs = scanners.scan_interfaces()
        self.iface.addItems(ifs or ["(keins)"])
        if iface:
            if iface not in ifs:
                self.iface.addItem(iface)
            self.iface.setCurrentText(iface)
        tb.addWidget(self.iface)
        tb.addWidget(QLabel("  Intervall: "))
        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setValue(int(self.settings.value("interval", 3)))
        self.interval.setSuffix(" s")
        tb.addWidget(self.interval)
        self.pause = QCheckBox("Pause")
        self.pause.setStyleSheet("QCheckBox:checked { color: #e04040; font-weight: bold; }")
        self.pause.setToolTip("WLAN-Scan anhalten, die Netze bleiben sichtbar.\n"
                              "Wird beim Start des Monitormodus automatisch gesetzt und beim Stopp "
                              "wieder aufgehoben.")
        tb.addWidget(self.pause)
        self._paused_by_monitor = False
        self.scan_info = QLabel("")
        self.scan_info.setStyleSheet("color: #e0a040; padding-left: 12px;")
        tb.addWidget(self.scan_info)

        self.source.currentTextChanged.connect(self._restart_scanner)
        self.iface.currentTextChanged.connect(self._restart_scanner)
        self.interval.valueChanged.connect(lambda v: setattr(self.worker, "interval", float(v)))
        self.pause.toggled.connect(self._on_pause)

    def _build_central(self) -> None:
        split = QSplitter(Qt.Orientation.Vertical)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(4, 4, 4, 0)
        fl = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter: SSID, BSSID, Hersteller, Sicherheit, Kanal …")
        self.filter.setClearButtonEnabled(True)
        fl.addWidget(self.filter, 1)
        self.only_band = QCheckBox("nur aktuelles Band")
        fl.addWidget(self.only_band)
        fl.addWidget(QLabel("  SSIDs:"))
        self.ssid_mode = QComboBox()
        for key, text in MODES:
            self.ssid_mode.addItem(text, key)
        self.ssid_mode.setToolTip("Filter über die eigene SSID-Liste (★): nur diese Netze anzeigen oder "
                                  "sie ausblenden – gilt für Tabelle, Kanalgraph und Signalverlauf")
        i = self.ssid_mode.findData(str(self.settings.value("ssid_list/mode", "all")))
        self.ssid_mode.setCurrentIndex(max(i, 0))
        fl.addWidget(self.ssid_mode)
        self.ssid_btn = QPushButton("Liste …")
        self.ssid_btn.setToolTip("SSID-Liste bearbeiten (Rechtsklick in der Tabelle: SSID hinzufügen/entfernen)")
        fl.addWidget(self.ssid_btn)
        tl.addLayout(fl)

        self.model = ApTableModel(self)
        self.model.marked = self.ssid_list.matches
        self.proxy = ApFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(8, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for col, w in enumerate([24, 170, 130, 170, 60, 70, 70, 85, 90, 190, 80, 80, 60, 70]):
            self.table.setColumnWidth(col, w)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(self._zoom_to_row)
        self.table.setToolTip("Doppelklick: Spektrum auf den Kanal dieses APs zoomen")
        tl.addWidget(self.table)
        split.addWidget(top)

        self.tabs = QTabWidget()
        self.panels: dict[str, BandPanel] = {}
        for key in self.BAND_KEYS:
            p = BandPanel(key)
            p.apClicked.connect(self._select_bssid)
            self.panels[key] = p
            self.tabs.addTab(p, p.band.name)
        self.history = HistoryWidget()
        self.tabs.addTab(self.history, "Signalverlauf")
        self.details = InfoPage("AP in der Tabelle oder im Kanalgraph auswählen, um Details zu sehen.",
                                raw=True)
        self.tabs.addTab(self.details, "Details")
        self.conn_page = InfoPage("Verbindung wird abgefragt …")
        self.tabs.addTab(self.conn_page, "Verbindung")
        self.monitor_page = MonitorPage(self.settings)
        self.monitor_page.updated.connect(self._on_monitor)
        self.tabs.addTab(self.monitor_page, "Monitor")
        self.monitor_page.alert.connect(self._on_monitor_alert)
        self.monitor_page.runningChanged.connect(self._on_monitor_running)
        split.addWidget(self.tabs)
        split.setSizes([300, 650])
        self.setCentralWidget(split)

        self.filter.textChanged.connect(self.proxy.set_text)
        self.ssid_mode.currentIndexChanged.connect(lambda _: self._apply_ssid_filter())
        self.ssid_btn.clicked.connect(self._edit_ssid_list)
        self._apply_ssid_filter(refresh=False)
        self.only_band.toggled.connect(lambda _: self._update_band_filter())
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.table.selectionModel().selectionChanged.connect(self._on_table_selection)

    def _build_sdr_dock(self) -> None:
        self.sdr_panel = SdrPanel()
        scroll = QScrollArea()
        scroll.setWidget(self.sdr_panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        dock = QDockWidget("SDR", self)
        dock.setObjectName("sdr_dock")
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.sdr_dock = dock
        # Schließt man das Panel (X oben rechts), holt dieser Knopf es zurück
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)
        act = dock.toggleViewAction()
        act.setText("SDR-Panel")
        act.setShortcut("F9")
        act.setToolTip("SDR-Einstellungen ein-/ausblenden (F9)")
        self.toolbar.addAction(act)

        sp = self.sdr_panel
        s = self.settings
        sp.wf_min.setValue(float(s.value("disp/wf_min", -100)))
        sp.wf_max.setValue(float(s.value("disp/wf_max", -40)))
        sp.cmap.setCurrentText(str(s.value("disp/cmap", "inferno")))
        sp.rows.setValue(int(s.value("disp/rows", 300)))
        i = sp.fps.findData(int(s.value("disp/fps", 25)))
        sp.fps.setCurrentIndex(max(i, 0))
        sp.set_curve_colors({k: QColor(str(s.value(f"disp/color_{k}")))
                             for k in ("live", "peak", "avg") if s.value(f"disp/color_{k}")})
        sp.cb_fill.setChecked(s.value("disp/fill", False, type=bool))
        sp.fill_alpha.setValue(int(s.value("disp/fill_alpha", 50)))
        sp.live_avg.setValue(int(s.value("disp/live_avg", 0)))
        sp.ap_alpha.setValue(int(s.value("disp/ap_alpha", 100)))
        sp.backendChanged.connect(self._on_backend_changed)
        sp.runToggled.connect(self._on_run_toggled)
        sp.paramChanged.connect(self._on_param_changed)
        sp.displayChanged.connect(self._apply_display)
        sp.autoLevelRequested.connect(self._auto_levels)
        sp.peakResetRequested.connect(lambda: self.panels[self.sdr_band].reset_peak())
        sp.rangeChanged.connect(self._set_range)
        sp.rangePresetRequested.connect(self._range_preset)
        # andere Funkdienste (Überlagerung)
        raw = s.value("overlays/enabled")
        keys = set(overlays.DEFAULT_ON) if raw is None else {k for k in str(raw).split(",") if k}
        sp.set_enabled_overlays(keys & set(overlays.OVERLAY_BY_KEY))
        sp.overlaysChanged.connect(self._apply_overlays)
        self._apply_overlays()
        # automatische Signalerkennung
        def _b(key, default):
            v = s.value(key, default)
            return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
        try:
            thr = float(s.value("detect/threshold", 10.0))
        except (TypeError, ValueError):
            thr = 10.0
        sp.set_detect_settings(_b("detect/on", True), thr, _b("detect/marks", True),
                               _b("detect/marks_wlan", False))
        sp.detectChanged.connect(self._apply_detect)
        sp.detectResetRequested.connect(self._reset_detect)
        # Sweep-Bereich je Band (gespeichert)
        self.sdr_ranges: dict[str, tuple[float, float]] = {}
        for key, panel in self.panels.items():
            b = panel.band
            try:
                lo = float(s.value(f"range/{key}/lo", b.f_min))
                hi = float(s.value(f"range/{key}/hi", b.f_max))
            except (TypeError, ValueError):
                lo, hi = b.f_min, b.f_max
            lo, hi = max(lo, b.f_min), min(hi, b.f_max)
            if hi - lo < 5:
                lo, hi = b.f_min, b.f_max
            self.sdr_ranges[key] = (lo, hi)
            panel.set_sdr_range(lo, hi)
        self._apply_display()
        self._apply_detect()
        self.det_timer = QTimer(self)
        self.det_timer.timeout.connect(self._update_findings)
        self.det_timer.start(500)

    # ------------------------------------------------------------------ WLAN
    def _make_scanner(self) -> scanners.Scanner:
        src = self.source.currentText()
        iface = self.iface.currentText()
        iface = None if iface.startswith("(") else iface
        if src == "auto":
            if not scanners.scan_interfaces() and scanners.wireless_interfaces():
                # nur Monitor-Interfaces vorhanden: Netze aus dem Monitormodus
                return scanners.MonitorScanner(self.monitor_page.backend)
            return scanners.auto_scanner(iface)
        if src == "monitor":
            return scanners.MonitorScanner(self.monitor_page.backend)
        return scanners.SCANNERS[src](iface)

    def _restart_scanner(self) -> None:
        self.worker.scanner = self._make_scanner()
        if self.monitor_page.backend.running:
            # Quelle „monitor“ füllt die Tabelle aus dem Monitor – dann nicht pausieren
            if self.worker.scanner.name == "monitor" and self._paused_by_monitor:
                self.pause.setChecked(False)
            elif self.worker.scanner.name != "monitor" and not self.pause.isChecked():
                self.pause.setChecked(True)
                self._paused_by_monitor = True
        self.scan_info.setText(self.worker.scanner.status)

    def _on_scan(self, aps: list) -> None:
        self.monitor_page.set_names(aps)
        self._annotate_monitor(aps)
        self.aps = aps
        self.model.set_aps(aps)
        self._push_aps()
        self.history.add_scan(aps)
        self._reselect()
        self._update_details()
        if self._link is not None:
            self._show_link()
        counts = {k: sum(1 for a in aps if a.band == k) for k in self.BAND_KEYS}
        self.status_aps.setText(
            f"{len(aps)} APs  (2.4 GHz: {counts['2.4']}, 5 GHz: {counts['5']}, 6 GHz: {counts['6']})"
            f"   Quelle: {self.worker.scanner.name}   {time.strftime('%H:%M:%S')}")
        self.scan_info.setText(self.worker.scanner.status)

    # ------------------------------------------------------------------ Monitormodus
    def _annotate_monitor(self, aps: list) -> None:
        """Monitor-Daten (Clients, Beacons …) an die APs eines normalen Scans hängen."""
        snap = self.monitor_page.backend.snapshot if self.monitor_page.backend.running else None
        for ap in aps:
            if ap.extra.get("source") == "monitor":
                continue
            d = snap["bss"].get(ap.bssid) if snap else None
            if d is None:
                ap.extra.pop("mon", None)
            else:
                ap.extra["mon"] = d

    def _on_pause(self, on: bool) -> None:
        self.worker.paused = on
        if not on:
            self._paused_by_monitor = False

    def _on_monitor_running(self, running: bool) -> None:
        """Monitor läuft: normalen Scan anhalten (Netze bleiben stehen), danach wieder fortsetzen."""
        if running:
            if self.worker.scanner.name != "monitor" and not self.pause.isChecked():
                self.pause.setChecked(True)
                self._paused_by_monitor = True
        elif self._paused_by_monitor:
            self.pause.setChecked(False)          # setzt auch _paused_by_monitor zurück

    def _on_monitor_alert(self, n: int) -> None:
        i = self.tabs.indexOf(self.monitor_page)
        self.tabs.setTabText(i, f"Monitor ⚠ {n}" if n else "Monitor")
        self.tabs.tabBar().setTabTextColor(i, QColor(230, 90, 60) if n else QColor())

    def _on_monitor(self) -> None:
        snap = self.monitor_page.backend.snapshot
        if snap is None:
            return
        self.history.add_samples(snap.get("samples", []))
        snap["samples"] = []
        if self.aps and self.worker.scanner.name != "monitor":
            self._annotate_monitor(self.aps)
            if self.tabs.currentWidget() is self.details:
                self._update_details()

    def _on_scan_failed(self, msg: str) -> None:
        self.scan_info.setText(msg)

    # ------------------------------------------------------------------ SSID-Liste
    def _ssid_ok(self, ssid: str) -> bool:
        return self.ssid_list.accepts(ssid, self.ssid_mode.currentData())

    def _push_aps(self) -> None:
        """Gefilterte APs an die Kanalgraphen (Signalerkennung bekommt immer alle)."""
        visible = [a for a in self.aps if self._ssid_ok(a.ssid)]
        for p in self.panels.values():
            p.set_aps(visible, self.aps)

    def _apply_ssid_filter(self, refresh: bool = True) -> None:
        self.settings.setValue("ssid_list/mode", self.ssid_mode.currentData())
        self.proxy.set_ssid_filter(self._ssid_ok)
        self.history.accept_ssid = self._ssid_ok
        if refresh:
            self._push_aps()
            self.history.redraw()
            self.model.layoutChanged.emit()

    def _edit_ssid_list(self) -> None:
        dlg = SsidListDialog(self.ssid_list.entries, [a.ssid for a in self.aps], self)
        if dlg.exec():
            self.ssid_list.set_entries(dlg.entries())
            self._apply_ssid_filter()

    def _table_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        ap = self.model.aps[self.proxy.mapToSource(idx).row()]
        menu = QMenu(self)
        if ap.ssid:
            if ap.ssid in self.ssid_list.entries:
                act = menu.addAction(f"„{ap.ssid}“ aus der SSID-Liste entfernen")
                act.triggered.connect(lambda: (self.ssid_list.remove(ap.ssid), self._apply_ssid_filter()))
            elif self.ssid_list.matches(ap.ssid):
                menu.addAction(f"„{ap.ssid}“ passt auf ein Muster der Liste").setEnabled(False)
            else:
                act = menu.addAction(f"„{ap.ssid}“ zur SSID-Liste hinzufügen")
                act.triggered.connect(lambda: (self.ssid_list.add(ap.ssid), self._apply_ssid_filter()))
        else:
            menu.addAction("Verstecktes Netz (keine SSID)").setEnabled(False)
        menu.addSeparator()
        menu.addAction("SSID-Liste bearbeiten …").triggered.connect(self._edit_ssid_list)
        menu.addAction("Details anzeigen").triggered.connect(
            lambda: (self._select_bssid(ap.bssid), self.tabs.setCurrentWidget(self.details)))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ Details / Verbindung
    def _update_details(self) -> None:
        ap = next((a for a in self.aps if a.bssid == self.selected), None)
        if ap is None:
            if self.selected is None:
                self.details.set_content("AP in der Tabelle oder im Kanalgraph auswählen, "
                                         "um Details zu sehen.", [])
            else:
                self.details.set_content(f"{self.selected}: nicht mehr im Scan", [])
            return
        self.details.set_content(f"{ap.display_ssid}   ({ap.bssid})", ap_details(ap), raw_text(ap))

    def _on_link(self, info) -> None:
        self._link = info
        self._show_link()

    def _show_link(self) -> None:
        title, sections = self.conn_view.sections(self._link, self.aps)
        self.conn_page.set_content(title, sections)

    # ------------------------------------------------------------------ Auswahl
    def _on_table_selection(self, *_):
        rows = self.table.selectionModel().selectedRows()
        bssid = None
        if rows:
            src = self.proxy.mapToSource(rows[0])
            bssid = self.model.aps[src.row()].bssid
        self._set_selected(bssid)

    def _set_selected(self, bssid: str | None) -> None:
        self.selected = bssid
        for p in self.panels.values():
            p.set_selected(bssid)
        self.history.set_selected(bssid)
        self._update_details()

    def _select_bssid(self, bssid: str) -> None:
        for r, ap in enumerate(self.model.aps):
            if ap.bssid == bssid:
                idx = self.proxy.mapFromSource(self.model.index(r, 0))
                if idx.isValid():
                    self.table.selectRow(idx.row())
                    self.table.scrollTo(idx)
                else:
                    self._set_selected(bssid)
                return

    def _reselect(self) -> None:
        if self.selected is None:
            return
        self.table.selectionModel().blockSignals(True)
        self._select_bssid(self.selected)
        self.table.selectionModel().blockSignals(False)
        self._set_selected(self.selected)

    def _update_band_filter(self) -> None:
        idx = self.tabs.currentIndex()
        band = self.BAND_KEYS[idx] if idx < len(self.BAND_KEYS) else None
        self.proxy.set_band(band if self.only_band.isChecked() else None)
        self.history.band_filter = band if self.only_band.isChecked() else None
        self.history.redraw()

    def _on_tab_changed(self, idx: int) -> None:
        self._update_band_filter()
        if idx < len(self.BAND_KEYS):
            band = self.BAND_KEYS[idx]
            if band != self.sdr_band:
                self.sdr_band = band
                self._sync_range_ui()
                if self.backend is not None and self.backend.running:
                    self._start_sdr()

    # ------------------------------------------------------------------ SDR
    def _create_backend(self, name: str) -> SdrBackend:
        cls = BACKENDS[name]
        self.settings.beginGroup(f"sdr/{name}")
        values = {p.key: _coerce(p, self.settings.value(p.key)) for p in cls.params}
        self.settings.endGroup()
        if cls is DemoBackend:
            return DemoBackend(values, ap_source=lambda: self.aps)
        return cls(values)

    def _on_backend_changed(self, name: str) -> None:
        was_running = self.backend is not None and self.backend.running
        if self.backend is not None:
            self.backend.close()
        self.backend = self._create_backend(name)
        self.settings.setValue("sdr/backend", name)
        off = self.settings.value(f"sdr/{name}/offset")
        if off is not None:
            self.sdr_panel.offset.setValue(float(off))
        self.sdr_panel.build_params(self.backend.params, self.backend.settings)
        self._apply_display()
        if was_running:
            self._start_sdr()
        else:
            self.sdr_panel.set_running(False)

    def _start_sdr(self) -> None:
        ok, why = self.backend.available()
        if not ok:
            self.sdr_panel.set_running(False)
            self.sdr_panel.status.setText(why)
            return
        lo, hi = self.sdr_ranges[self.sdr_band]
        for p in self.panels.values():
            p.reset_sdr()
        self._drain()
        self._sweep_times.clear()
        self.backend.start(lo, hi)
        lo, hi = self.backend.range
        self.sdr_panel.status.setText(f"Sweep {lo:.0f}–{hi:.0f} MHz …")
        self.sdr_panel.set_running(True)

    # -- Signalerkennung ----------------------------------------------------
    def _apply_detect(self) -> None:
        on, thr, marks, mark_wlan = self.sdr_panel.detect_settings()
        for p in self.panels.values():
            p.set_detection(on, thr, marks, mark_wlan)
        s = self.settings
        s.setValue("detect/on", on)
        s.setValue("detect/threshold", thr)
        s.setValue("detect/marks", marks)
        s.setValue("detect/marks_wlan", mark_wlan)
        self._update_findings()

    def _reset_detect(self) -> None:
        for p in self.panels.values():
            p.reset_detection()
            p.wf_dirty = True
        self._update_findings()

    def _update_findings(self) -> None:
        if self.sdr_band not in self.panels:
            return
        panel = self.panels[self.sdr_band]
        found = panel.clf.findings(time.time()) if panel.detect_on else []
        self.sdr_panel.set_findings(found, self.sdr_panel.offset.value())

    def _apply_overlays(self) -> None:
        keys = self.sdr_panel.enabled_overlays()
        for p in self.panels.values():
            p.set_overlays(keys)
        self.settings.setValue("overlays/enabled", ",".join(sorted(keys)))

    # -- Sweep-Bereich ------------------------------------------------------
    def _sync_range_ui(self) -> None:
        if self.sdr_band not in self.panels:
            return
        self.sdr_panel.show_overlays_for_band(self.sdr_band)
        b = self.panels[self.sdr_band].band
        self.sdr_panel.set_range_limits(b.f_min, b.f_max, *self.sdr_ranges[self.sdr_band])

    def _set_range(self, lo: float, hi: float) -> None:
        panel = self.panels[self.sdr_band]
        b = panel.band
        lo, hi = max(lo, b.f_min), min(hi, b.f_max)
        if hi - lo < 5:                     # Mindestbreite
            c = min(max((lo + hi) / 2, b.f_min + 2.5), b.f_max - 2.5)
            lo, hi = c - 2.5, c + 2.5
        if (lo, hi) == self.sdr_ranges[self.sdr_band]:
            return
        self.sdr_ranges[self.sdr_band] = (lo, hi)
        self.settings.setValue(f"range/{self.sdr_band}/lo", lo)
        self.settings.setValue(f"range/{self.sdr_band}/hi", hi)
        panel.set_sdr_range(lo, hi)
        self._sync_range_ui()
        if self.backend is not None and self.backend.running:
            self._start_sdr()

    def _range_preset(self, which: str) -> None:
        panel = self.panels[self.sdr_band]
        b = panel.band
        if which == "band":
            self._set_range(b.f_min, b.f_max)
        elif which == "view":
            self._set_range(*panel.view_range())
        elif which == "ap":
            ap = next((a for a in self.aps if a.bssid == self.selected), None)
            if ap is None or ap.band != self.sdr_band:
                self.sdr_panel.status.setText("Kein AP in diesem Band ausgewählt")
                return
            self._zoom_to_ap(ap)

    def _zoom_to_row(self, idx) -> None:
        """Doppelklick in der Tabelle: Band-Tab des APs zeigen und auf seinen Kanal zoomen."""
        src = self.proxy.mapToSource(idx)
        if not src.isValid() or src.row() >= len(self.model.aps):
            return
        ap = self.model.aps[src.row()]
        if ap.band not in self.panels:
            return
        self._set_selected(ap.bssid)
        self.tabs.setCurrentWidget(self.panels[ap.band])     # setzt auch sdr_band
        self._zoom_to_ap(ap)

    def _zoom_to_ap(self, ap) -> None:
        """Sweep-Bereich und Ansicht auf die belegte Bandbreite des APs (mit etwas Rand)."""
        width = float(ap.width or 20)
        center = float(ap.center_freq or ap.freq)
        m = max(5.0, width * 0.25)
        self._set_range(center - width / 2 - m, center + width / 2 + m)
        # auch zoomen, wenn der Sweep-Bereich schon stimmte, die Ansicht aber verschoben wurde
        self.panels[ap.band].set_sdr_range(*self.sdr_ranges[ap.band])
        # Pegel anpassen, sobald der neue Bereich genug Zeilen im Wasserfall hat
        if self.backend is not None and self.backend.running:
            self._auto_lv_until = time.monotonic() + 15.0

    def _drain(self) -> None:
        if self.backend is None:
            return
        while not self.backend.sweeps.empty():
            self.backend.sweeps.get_nowait()

    def _on_run_toggled(self, on: bool) -> None:
        if on:
            self._start_sdr()
        else:
            self.backend.stop()
            self._drain()
            self.sdr_panel.status.setText("gestoppt")
            self.status_sdr.setText("SDR aus")
        self.sdr_panel.set_running(self.backend.running if on else False)

    def _on_param_changed(self, key: str, value) -> None:
        if self.backend is None:
            return
        self.settings.setValue(f"sdr/{self.backend.name}/{key}", value)
        live = self.backend.update_setting(key, value)
        if not live and self.backend.running:
            self._start_sdr()

    def _apply_display(self) -> None:
        sp = self.sdr_panel
        self._frame_dt = 1.0 / float(sp.fps.currentData() or 25)
        for p in self.panels.values():
            p.offset = sp.offset.value()
            p.set_levels(sp.wf_min.value(), sp.wf_max.value())
            p.set_colormap(sp.cmap.currentText())
            p.set_rows(sp.rows.value())
            p.show_live = sp.cb_live.isChecked()
            p.show_peak = sp.cb_peak.isChecked()
            p.show_avg = sp.cb_avg.isChecked()
            p.set_curve_style(sp.curve_colors(), sp.cb_fill.isChecked(), sp.fill_alpha.value() / 100)
            p.set_ap_intensity(sp.ap_alpha.value() / 100)
            p.set_live_averaging(sp.live_avg.value())
            p.wf_dirty = True
        if self.backend is not None:
            self.settings.setValue(f"sdr/{self.backend.name}/offset", sp.offset.value())

    AUTO_LV_ROWS = 20                       # Wasserfall-Zeilen vor der automatischen Pegelanpassung

    def _auto_levels(self) -> None:
        lv = self.panels[self.sdr_band].auto_levels()
        if lv:
            self.sdr_panel.wf_min.setValue(round(lv[0]))
            self.sdr_panel.wf_max.setValue(round(lv[1]))

    def _maybe_refresh(self, panel) -> None:
        """Neu zeichnen höchstens mit der eingestellten Bildrate (Daten laufen im 40-ms-Takt)."""
        now = time.monotonic()
        if now - self._frame_t >= self._frame_dt - 0.005:
            self._frame_t = now
            panel.refresh()

    def _poll_sdr(self) -> None:
        b = self.backend
        if b is None:
            return
        while not b.errors.empty():
            msg = b.errors.get_nowait()
            self.sdr_panel.status.setText(msg)
            self.status_sdr.setText("SDR-Fehler")
            self.sdr_panel.set_running(False)
        panel = self.panels[self.sdr_band]
        batch = []
        while not b.sweeps.empty():
            sw = b.sweeps.get_nowait()
            batch.append((sw.freqs_mhz, sw.power_db))
            self._sweep_times.append(sw.timestamp)
            self._last_sweep = sw
        n = len(batch)
        if n:
            # alles seit dem letzten Takt ergibt eine Zeile (sehr schnelle Sweeps bei schmalem Bereich)
            panel.push_sweeps(batch, self._last_sweep.timestamp)
            if self._auto_lv_until:
                if panel.wf_count >= self.AUTO_LV_ROWS:
                    self._auto_lv_until = 0.0
                    self._auto_levels()
                elif time.monotonic() > self._auto_lv_until:
                    self._auto_lv_until = 0.0
            self._maybe_refresh(panel)
            now = time.time()
            if now - self._status_t < 0.5:     # Statuszeile nur 2×/s (spart Neuzeichnen)
                return
            self._status_t = now
            self._sweep_times = [t for t in self._sweep_times if t > now - 2.0]
            rate = len(self._sweep_times) / 2.0
            sw = self._last_sweep
            txt = (f"{b.name}: {rate:.1f} Sweeps/s, {len(sw.freqs_mhz)} Bins, "
                   f"{sw.f_lo:.0f}–{sw.f_hi:.0f} MHz")
            restarts = getattr(b, "restarts", 0)
            if restarts:
                txt += f", {restarts}× neu gestartet"
            self.sdr_panel.status.setText(txt)
            self.status_sdr.setText(txt)
        else:
            self._maybe_refresh(panel)
            if b.running and b.state:
                self.sdr_panel.status.setText(b.state)

    # ------------------------------------------------------------------ Ende
    def closeEvent(self, ev) -> None:
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("band", self.sdr_band)
        s.setValue("interval", self.interval.value())
        sp = self.sdr_panel
        s.setValue("disp/wf_min", sp.wf_min.value())
        s.setValue("disp/wf_max", sp.wf_max.value())
        s.setValue("disp/cmap", sp.cmap.currentText())
        s.setValue("disp/rows", sp.rows.value())
        s.setValue("disp/fps", sp.fps.currentData())
        for k, c in sp.curve_colors().items():
            s.setValue(f"disp/color_{k}", c.name(QColor.NameFormat.HexArgb))
        s.setValue("disp/fill", sp.cb_fill.isChecked())
        s.setValue("disp/fill_alpha", sp.fill_alpha.value())
        s.setValue("disp/live_avg", sp.live_avg.value())
        s.setValue("disp/ap_alpha", sp.ap_alpha.value())
        if self.backend is not None:
            self.backend.close()
        self.monitor_page._save()
        self.monitor_page.stop()
        self.worker.stop()
        super().closeEvent(ev)
