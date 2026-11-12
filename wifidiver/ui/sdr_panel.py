"""Steuerpanel für das SDR (Gerät, Parameter, Wasserfall-Darstellung)."""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                             QSlider, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..sdr import BACKENDS
from ..sdr.base import Param
from ..wifi.channels import freq_to_channel
from ..wifi.overlays import OVERLAYS

MIN_SPAN_MHZ = 5

COLORMAPS = ["inferno", "viridis", "magma", "plasma", "turbo", "CET-L8", "CET-L16"]
DEFAULT_OFFSET = {"HackRF": -30.0, "UHD": -20.0, "Demo": 0.0}


FPS_CHOICES = (25, 15, 10, 5)

class SdrPanel(QWidget):
    backendChanged = pyqtSignal(str)
    runToggled = pyqtSignal(bool)
    paramChanged = pyqtSignal(str, object)
    displayChanged = pyqtSignal()
    autoLevelRequested = pyqtSignal()
    peakResetRequested = pyqtSignal()
    rangeChanged = pyqtSignal(float, float)      # neuer Sweep-Bereich (MHz)
    rangePresetRequested = pyqtSignal(str)       # "band", "ap", "view"
    overlaysChanged = pyqtSignal()               # Auswahl anderer Funkdienste geändert
    detectChanged = pyqtSignal()                 # Einstellungen der Signalerkennung geändert
    detectResetRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)

        # Gerät -----------------------------------------------------------
        dev = QGroupBox("SDR-Gerät")
        dl = QVBoxLayout(dev)
        row = QHBoxLayout()
        self.backend_combo = QComboBox()
        for name, cls in BACKENDS.items():
            ok, why = cls.available()
            self.backend_combo.addItem(name)
            if not ok:
                i = self.backend_combo.count() - 1
                self.backend_combo.setItemData(i, why, 3)  # ToolTipRole
        row.addWidget(self.backend_combo, 1)
        self.run_btn = QPushButton("Start")
        self.run_btn.setCheckable(True)
        row.addWidget(self.run_btn)
        dl.addLayout(row)
        self.avail_label = QLabel("")
        self.avail_label.setWordWrap(True)
        self.avail_label.setStyleSheet("color: #e0a040;")
        dl.addWidget(self.avail_label)
        self.status = QLabel("gestoppt")
        self.status.setStyleSheet("color: #9a9a9a;")
        dl.addWidget(self.status)
        lay.addWidget(dev)

        # Sweep-Bereich ----------------------------------------------------
        rng = QGroupBox("Sweep-Bereich")
        rl = QVBoxLayout(rng)
        self.sl_min = QSlider(Qt.Orientation.Horizontal)
        self.sl_max = QSlider(Qt.Orientation.Horizontal)
        self.lbl_min = QLabel()
        self.lbl_max = QLabel()
        self.lbl_span = QLabel()
        self.lbl_span.setStyleSheet("color: #9a9a9a;")
        for text, sl, lbl in (("min", self.sl_min, self.lbl_min), ("max", self.sl_max, self.lbl_max)):
            r = QHBoxLayout()
            name = QLabel(text)
            name.setFixedWidth(28)
            r.addWidget(name)
            r.addWidget(sl, 1)
            lbl.setMinimumWidth(118)
            r.addWidget(lbl)
            rl.addLayout(r)
            sl.setSingleStep(1)
            sl.setPageStep(20)
            sl.valueChanged.connect(lambda v, which=text: self._slider_moved(which))
        rl.addWidget(self.lbl_span)
        br = QHBoxLayout()
        for key, label, tip in (("band", "Ganzes Band", "Ganzes Band sweepen"),
                                ("ap", "Ausgew. AP", "Kanal des ausgewählten AP (mit etwas Rand)"),
                                ("view", "Sichtbereich", "Aktuellen Zoom des Kanalgraphen übernehmen")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=key: self.rangePresetRequested.emit(k))
            br.addWidget(b)
        rl.addLayout(br)
        lay.addWidget(rng)
        self._range_timer = QTimer(self)
        self._range_timer.setSingleShot(True)
        self._range_timer.setInterval(400)     # erst nach kurzer Pause neu starten
        self._range_timer.timeout.connect(self._emit_range)

        self.param_box = QGroupBox("Parameter")
        self.param_form = QFormLayout(self.param_box)
        lay.addWidget(self.param_box)
        self.param_widgets: dict[str, QWidget] = {}

        # Darstellung -----------------------------------------------------
        disp = QGroupBox("Darstellung")
        f = QFormLayout(disp)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-200, 200)
        self.offset.setSingleStep(1)
        self.offset.setSuffix(" dB")
        self.offset.setToolTip("Wird zu den SDR-Werten addiert, um sie grob auf dBm zu kalibrieren")
        f.addRow("Pegel-Offset", self.offset)
        self.wf_min = QDoubleSpinBox()
        self.wf_max = QDoubleSpinBox()
        for w, v in ((self.wf_min, -100), (self.wf_max, -40)):
            w.setRange(-200, 50)
            w.setValue(v)
            w.setSuffix(" dB")
        f.addRow("Wasserfall min", self.wf_min)
        f.addRow("Wasserfall max", self.wf_max)
        self.auto_btn = QPushButton("Auto-Pegel")
        f.addRow("", self.auto_btn)
        self.cmap = QComboBox()
        self.cmap.addItems(COLORMAPS)
        f.addRow("Farbskala", self.cmap)
        self.rows = QSpinBox()
        self.rows.setRange(50, 2000)
        self.rows.setValue(300)
        self.rows.setSuffix(" Zeilen")
        f.addRow("Verlauf", self.rows)
        self.fps = QComboBox()
        for v in FPS_CHOICES:
            self.fps.addItem(f"{v} Bilder/s", v)
        self.fps.setToolTip("Wie oft Spektrum und Wasserfall neu gezeichnet werden. Weniger Bilder/s "
                            "sparen deutlich CPU (z.B. Raspberry Pi, ältere Notebooks).\n"
                            "Daten und Signalerkennung laufen unverändert weiter (25 Zeilen/s).")
        f.addRow("Bildrate", self.fps)
        self.fps.currentIndexChanged.connect(self.displayChanged)
        self.cb_live = QCheckBox("Live-Spektrum")
        self.cb_peak = QCheckBox("Peak-Hold")
        self.cb_avg = QCheckBox("Mittelwert")
        self.cb_live.setChecked(True)
        self.cb_peak.setChecked(True)
        for cb in (self.cb_live, self.cb_peak, self.cb_avg):
            f.addRow("", cb)
            cb.toggled.connect(self.displayChanged)
        self.peak_btn = QPushButton("Peak zurücksetzen")
        f.addRow("", self.peak_btn)
        lay.addWidget(disp)

        # Automatische Signalerkennung -----------------------------------------
        det = QGroupBox("Signalerkennung")
        dl2 = QVBoxLayout(det)
        r = QHBoxLayout()
        self.cb_detect = QCheckBox("automatisch erkennen")
        self.cb_detect.setChecked(True)
        r.addWidget(self.cb_detect, 1)
        self.det_reset = QPushButton("Leeren")
        self.det_reset.setToolTip("Erkannte Signale und Markierungen verwerfen")
        r.addWidget(self.det_reset)
        dl2.addLayout(r)
        ff = QFormLayout()
        self.det_thr = QDoubleSpinBox()
        self.det_thr.setRange(4, 30)
        self.det_thr.setSingleStep(1)
        self.det_thr.setDecimals(0)
        self.det_thr.setValue(10)
        self.det_thr.setSuffix(" dB")
        self.det_thr.setToolTip("Ein Burst muss so weit über dem gleitenden Rauschboden liegen")
        ff.addRow("Schwelle über Rauschen", self.det_thr)
        dl2.addLayout(ff)
        self.cb_marks = QCheckBox("im Wasserfall markieren")
        self.cb_marks.setChecked(True)
        self.cb_marks.setToolTip("Punkt = schmaler Burst, Quadrat = breiter Burst; "
                                 "Mauszeiger darauf zeigt Details")
        self.cb_marks_wlan = QCheckBox("auch WLAN markieren")
        dl2.addWidget(self.cb_marks)
        dl2.addWidget(self.cb_marks_wlan)
        self.det_tree = QTreeWidget()
        self.det_tree.setColumnCount(3)
        self.det_tree.setHeaderLabels(["Signal / Frequenzen", "pro s", "Pegel"])
        self.det_tree.setRootIsDecorated(False)
        self.det_tree.setIndentation(14)
        self.det_tree.setItemsExpandable(False)
        self.det_tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.det_tree.setMinimumHeight(150)
        self.det_tree.setWordWrap(True)
        hh = self.det_tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        dl2.addWidget(self.det_tree)
        note = QLabel("Einordnung über Frequenzraster, Breite, Dauer und Wiederholung der letzten "
                      "5 s – keine Dekodierung. Kurze Bursts werden nur erfasst, wenn der Sweep "
                      "gerade auf ihrer Frequenz steht.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #9a9a9a; font-size: 8pt;")
        dl2.addWidget(note)
        lay.addWidget(det)
        for cb in (self.cb_detect, self.cb_marks, self.cb_marks_wlan):
            cb.toggled.connect(self._detect_toggled)
        self.det_thr.valueChanged.connect(self.detectChanged)
        self.det_reset.clicked.connect(self.detectResetRequested)

        # Andere Funkdienste in den WLAN-Bändern ---------------------------------
        self.ovl_box = QGroupBox("Andere Funkdienste")
        ol = QVBoxLayout(self.ovl_box)
        self.ovl_checks: dict[str, QCheckBox] = {}
        self.ovl_band: dict[str, str] = {}
        for o in OVERLAYS:
            cb = QCheckBox(o.name)
            r, g, b = o.color
            cb.setStyleSheet(f"QCheckBox {{ color: rgb({r},{g},{b}); }}")
            cb.setToolTip(o.hint)
            cb.toggled.connect(self.overlaysChanged)
            ol.addWidget(cb)
            self.ovl_checks[o.key] = cb
            self.ovl_band[o.key] = o.band
        self.ovl_none = QLabel("")
        self.ovl_none.setStyleSheet("color: #9a9a9a;")
        ol.addWidget(self.ovl_none)
        lay.addWidget(self.ovl_box)
        lay.addStretch(1)

        self.backend_combo.currentTextChanged.connect(self._backend_changed)
        self.run_btn.toggled.connect(self._run_toggled)
        for w in (self.offset, self.wf_min, self.wf_max, self.rows):
            w.valueChanged.connect(self.displayChanged)
        self.cmap.currentTextChanged.connect(self.displayChanged)
        self.auto_btn.clicked.connect(self.autoLevelRequested)
        self.peak_btn.clicked.connect(self.peakResetRequested)

    # ------------------------------------------------------------------
    def _backend_changed(self, name: str) -> None:
        ok, why = BACKENDS[name].available()
        self.avail_label.setText("" if ok else why)
        self.avail_label.setVisible(not ok)
        self.offset.blockSignals(True)
        self.offset.setValue(DEFAULT_OFFSET.get(name, 0.0))
        self.offset.blockSignals(False)
        self.backendChanged.emit(name)

    # -- Signalerkennung ----------------------------------------------------
    def _detect_toggled(self, _=None) -> None:
        on = self.cb_detect.isChecked()
        for w in (self.det_thr, self.cb_marks, self.det_tree):
            w.setEnabled(on)
        self.cb_marks_wlan.setEnabled(on and self.cb_marks.isChecked())
        self.detectChanged.emit()

    def detect_settings(self) -> tuple[bool, float, bool, bool]:
        return (self.cb_detect.isChecked(), float(self.det_thr.value()),
                self.cb_marks.isChecked(), self.cb_marks_wlan.isChecked())

    def set_detect_settings(self, on: bool, thr: float, marks: bool, mark_wlan: bool) -> None:
        for w, v in ((self.cb_detect, on), (self.cb_marks, marks), (self.cb_marks_wlan, mark_wlan)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.det_thr.blockSignals(True)
        self.det_thr.setValue(thr)
        self.det_thr.blockSignals(False)
        self._detect_toggled()

    def set_findings(self, findings, offset: float = 0.0) -> None:
        """Ergebnisliste der Signalerkennung neu füllen."""
        t = self.det_tree
        if not findings:
            t.clear()
            it = QTreeWidgetItem(["(nichts erkannt)" if self.cb_detect.isChecked() else "(aus)",
                                  "", ""])
            it.setForeground(0, QBrush(QColor(150, 150, 150)))
            t.addTopLevelItem(it)
            return
        now = time.time()
        t.setUpdatesEnabled(False)
        t.clear()
        for fd in findings:
            rate = "läuft" if fd.active and fd.count <= 2 else (
                f"{fd.rate:.1f}" if fd.rate < 10 else f"{fd.rate:.0f}")
            it = QTreeWidgetItem([fd.name, rate, f"{fd.level + offset:.0f} dB"])
            col = QColor(*fd.color)
            if now - fd.last_seen > 2.5 and not fd.active:
                col = col.darker(150)      # zuletzt vor einer Weile gesehen
            it.setForeground(0, QBrush(col))
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            tip = (f"{fd.hint}\n\n{fd.where}\n{fd.count} Bursts in den letzten 5 s, zuletzt vor "
                   f"{max(0.0, now - fd.last_seen):.1f} s, Spitzenpegel {fd.level + offset:.0f} dB")
            for c in range(3):
                it.setToolTip(c, tip)
            it.setTextAlignment(1, Qt.AlignmentFlag.AlignRight)
            it.setTextAlignment(2, Qt.AlignmentFlag.AlignRight)
            t.addTopLevelItem(it)
            sub = QTreeWidgetItem([fd.where])
            sub.setForeground(0, QBrush(QColor(130, 130, 130)))
            sub.setToolTip(0, tip)
            it.addChild(sub)
            sub.setFirstColumnSpanned(True)
            it.setExpanded(True)
        t.setUpdatesEnabled(True)

    # -- andere Funkdienste -------------------------------------------------
    def enabled_overlays(self) -> set[str]:
        return {k for k, cb in self.ovl_checks.items() if cb.isChecked()}

    def set_enabled_overlays(self, keys) -> None:
        for k, cb in self.ovl_checks.items():
            cb.blockSignals(True)
            cb.setChecked(k in keys)
            cb.blockSignals(False)

    def show_overlays_for_band(self, band: str | None) -> None:
        """Nur die Dienste des aktiven Bandes anzeigen (Auswahl bleibt erhalten)."""
        n = 0
        for k, cb in self.ovl_checks.items():
            vis = self.ovl_band[k] == band
            cb.setVisible(vis)
            n += vis
        self.ovl_box.setTitle(f"Andere Funkdienste ({band} GHz)" if band else "Andere Funkdienste")
        self.ovl_none.setText("" if n else "keine für dieses Band")
        self.ovl_none.setVisible(not n)

    # -- Sweep-Bereich ----------------------------------------------------
    def set_range_limits(self, f_min: float, f_max: float, lo: float, hi: float) -> None:
        """Slidergrenzen (Band) und aktuellen Bereich setzen, ohne Signal."""
        for sl in (self.sl_min, self.sl_max):
            sl.blockSignals(True)
            sl.setRange(int(f_min), int(round(f_max)))
        self.sl_min.setValue(int(round(lo)))
        self.sl_max.setValue(int(round(hi)))
        for sl in (self.sl_min, self.sl_max):
            sl.blockSignals(False)
        self._update_range_labels()

    def range_values(self) -> tuple[float, float]:
        return float(self.sl_min.value()), float(self.sl_max.value())

    def _slider_moved(self, which: str) -> None:
        lo, hi = self.sl_min.value(), self.sl_max.value()
        if hi - lo < MIN_SPAN_MHZ:   # Mindestbreite halten, den anderen Slider mitschieben
            if which == "min":
                hi = min(lo + MIN_SPAN_MHZ, self.sl_max.maximum())
                lo = hi - MIN_SPAN_MHZ
            else:
                lo = max(hi - MIN_SPAN_MHZ, self.sl_min.minimum())
                hi = lo + MIN_SPAN_MHZ
            for sl, v in ((self.sl_min, lo), (self.sl_max, hi)):
                sl.blockSignals(True)
                sl.setValue(v)
                sl.blockSignals(False)
        self._update_range_labels()
        self._range_timer.start()

    def _update_range_labels(self) -> None:
        lo, hi = self.range_values()
        self.lbl_min.setText(f"{lo:.0f} MHz (Kanal {freq_to_channel(lo)})")
        self.lbl_max.setText(f"{hi:.0f} MHz (Kanal {freq_to_channel(hi)})")
        full = (self.sl_min.value() == self.sl_min.minimum() and self.sl_max.value() == self.sl_max.maximum())
        self.lbl_span.setText(f"Breite {hi - lo:.0f} MHz" + ("  (ganzes Band)" if full else ""))

    def _emit_range(self) -> None:
        self.rangeChanged.emit(*self.range_values())

    def _run_toggled(self, on: bool) -> None:
        self.run_btn.setText("Stop" if on else "Start")
        self.runToggled.emit(on)

    def set_running(self, on: bool) -> None:
        self.run_btn.blockSignals(True)
        self.run_btn.setChecked(on)
        self.run_btn.setText("Stop" if on else "Start")
        self.run_btn.blockSignals(False)

    def build_params(self, params: list[Param], values: dict) -> None:
        while self.param_form.rowCount():
            self.param_form.removeRow(0)
        self.param_widgets.clear()
        for p in params:
            v = values.get(p.key, p.default)
            if p.kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(v))
                w.toggled.connect(lambda val, k=p.key: self.paramChanged.emit(k, val))
            elif p.kind == "int":
                w = QSpinBox()
                w.setRange(int(p.minimum), int(p.maximum))
                w.setSingleStep(int(p.step))
                w.setValue(int(v))
                w.setSuffix(p.suffix)
                w.setKeyboardTracking(False)
                w.valueChanged.connect(lambda val, k=p.key: self.paramChanged.emit(k, val))
            elif p.kind == "float":
                w = QDoubleSpinBox()
                w.setRange(p.minimum, p.maximum)
                w.setSingleStep(p.step)
                w.setDecimals(2)
                w.setValue(float(v))
                w.setSuffix(p.suffix)
                w.setKeyboardTracking(False)
                w.valueChanged.connect(lambda val, k=p.key: self.paramChanged.emit(k, val))
            elif p.kind == "choice":
                w = QComboBox()
                for c in p.choices:
                    w.addItem(str(c), c)
                idx = w.findData(v)
                w.setCurrentIndex(max(idx, 0))
                w.currentIndexChanged.connect(
                    lambda i, k=p.key, cw=w: self.paramChanged.emit(k, cw.itemData(i)))
            else:
                w = QLineEdit(str(v))
                w.editingFinished.connect(lambda k=p.key, lw=w: self.paramChanged.emit(k, lw.text()))
            if not p.live:
                w.setToolTip("Änderung startet den Sweep neu")
            self.param_widgets[p.key] = w
            self.param_form.addRow(p.label, w)

