"""Kanalgraph eines Bandes mit SDR-Spektrum und Wasserfall (gemeinsame Frequenzachse)."""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QLineF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QGradient, QImage, QLinearGradient, QPainterPath,
                         QPen)
from PyQt6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from ..sdr.base import GridMapper
from ..sdr.classify import CLASSES, SignalClassifier, format_duration
from ..wifi.channels import BANDS, DFS_CHANNELS_5G, band_channels, channel_to_freq, freq_to_channel, is_psc
from ..wifi import overlays as ovl
from ..wifi.model import AccessPoint
from .ap_table import ap_color

FLOOR = -100.0
GRID_BINS = 1500
# Standardfarben der Spektrumkurven (RGBA)
CURVE_COLORS = {"live": (230, 230, 230, 170), "peak": (255, 90, 90, 150), "avg": (90, 160, 255, 255)}


def fill_brush(color: QColor, alpha: float) -> QBrush:
    """Verlauf wie bei gqrx: oben an der Kurve kräftig, zum Boden hin fast durchsichtig."""
    g = QLinearGradient(0, 0, 0, 1)
    g.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)
    top, bottom = QColor(color), QColor(color)
    top.setAlphaF(max(0.0, min(1.0, alpha)))
    bottom.setAlphaF(max(0.0, min(1.0, alpha * 0.12)))
    g.setColorAt(0.0, bottom)     # Datenkoordinaten: y wächst nach oben
    g.setColorAt(1.0, top)
    return QBrush(g)


class ChannelAxis(pg.AxisItem):
    """Untere Achse mit Kanalnummern an den Mittenfrequenzen."""

    def __init__(self, band: str, **kw):
        super().__init__("bottom", **kw)
        self.band = band
        self.chan_freqs = [(channel_to_freq(c, band), c) for c in band_channels(band)]

    def tickValues(self, minVal, maxVal, size):
        vis = [f for f, _ in self.chan_freqs if minVal <= f <= maxVal]
        if not vis:
            return super().tickValues(minVal, maxVal, size)
        max_ticks = max(2, int(size / 28))
        step = 1
        while len(vis) / step > max_ticks:
            step *= 2
        major = vis[::step]
        return [(0, major)]

    def tickStrings(self, values, scale, spacing):
        return [str(freq_to_channel(v)) for v in values]


class TimeAxis(pg.AxisItem):
    """Linke Wasserfall-Achse: Alter der Zeile in Sekunden."""

    def __init__(self, panel: "BandPanel", **kw):
        super().__init__("left", **kw)
        self.panel = panel

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            age = self.panel.age_of_row(v)
            out.append("" if age is None else (f"{age:.0f}s" if age >= 10 else f"{age:.1f}s"))
        return out


def cache_item(item) -> None:
    """Statisches Element als Pixmap zwischenspeichern (inkl. Kinder).

    Das Live-Spektrum aktualisiert den Kanalgraphen bis zu 25-mal pro Sekunde; ohne Cache
    würden dabei jedes Mal alle AP-Kurven (geglättet, gefüllt), Overlay-Bereiche und das
    Gitter neu gerastert.
    """
    item.setCacheMode(item.CacheMode.DeviceCoordinateCache)
    for ch in item.childItems():
        cache_item(ch)


class SpansItem(pg.GraphicsObject):
    """Mehrere senkrechte Frequenzbereiche (oder Linien bei Breite 0) als EIN Element.

    Ersetzt je Bereich ein LinearRegionItem (= zwei InfiniteLines): Diese hängen an der Ansicht und
    wurden in jedem Bild einzeln neu gezeichnet. Hier liegt alles fest in Datenkoordinaten, ein
    Zeichenaufruf genügt und der Pixmap-Cache bleibt gültig, solange nicht gezoomt wird.
    """

    def __init__(self, spans, brush=None, pen=None, y0: float = -400.0, y1: float = 200.0):
        super().__init__()
        self.Y0, self.Y1 = y0, y1     # Standard deckt jeden sinnvollen Pegelbereich ab
        self.brush = QBrush(QColor(*brush)) if brush else None
        if pen is not None:
            pen = QPen(pen)
            pen.setCosmetic(True)    # 1 Pixel breit, unabhängig vom Zoom
        self.pen = pen
        self.set_spans(spans)

    def set_spans(self, spans) -> None:
        self.prepareGeometryChange()
        self.spans = [(float(a), float(b)) for a, b in spans] or [(0.0, 0.0)]
        lo = min(a for a, _ in self.spans)
        hi = max(b for _, b in self.spans)
        self._rect = QRectF(lo, self.Y0, max(hi - lo, 1e-6), self.Y1 - self.Y0)
        self._path = QPainterPath()
        for a, b in self.spans:
            self._path.addRect(QRectF(a, self.Y0, max(b - a, 1e-6), self.Y1 - self.Y0))
        self.update()

    def setRegion(self, rgn) -> None:          # wie LinearRegionItem
        self.set_spans([rgn])

    def boundingRect(self) -> QRectF:
        return self._rect

    def shape(self) -> QPainterPath:     # Tooltip nur über den Bereichen selbst
        return self._path

    def paint(self, p, *args) -> None:
        if self.brush is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.brush)
            for a, b in self.spans:
                if b > a:
                    p.drawRect(QRectF(a, self.Y0, b - a, self.Y1 - self.Y0))
        if self.pen is not None:
            p.setPen(self.pen)
            xs = sorted({x for ab in self.spans for x in ab})
            p.drawLines([QLineF(x, self.Y0, x, self.Y1) for x in xs])


class WaterfallItem(pg.GraphicsObject):
    """Wasserfallbild direkt aus einem Ringpuffer gezeichnet.

    Neue Zeilen werden einmal eingefärbt (Farbtabelle) und in einen ARGB-Ringpuffer geschrieben;
    beim Zeichnen werden die beiden Teile des Rings an die richtige Stelle gesetzt. So entfällt
    das Umsortieren und Neu-Einfärben des ganzen Bildes in jedem Anzeigetakt.
    Ringlayout: die neueste Zeile liegt bei ``pos``, ältere folgen bei pos+1, pos+2, … (modulo).
    """

    def __init__(self):
        super().__init__()
        self.rect = QRectF(0, 0, 1, 1)
        self.lut = np.zeros(256, dtype=np.uint32)
        self.lo, self.hi = -100.0, -40.0
        self.alloc(1, 1)

    def alloc(self, rows: int, bins: int) -> None:
        self.argb = np.zeros((rows, bins), dtype=np.uint32)
        self.argb[:] = self.lut[0] if len(self.lut) else 0
        self.qimg = QImage(self.argb.data, bins, rows, bins * 4, QImage.Format.Format_RGB32)
        self.pos = 0

    def set_rect(self, rect: QRectF) -> None:
        self.prepareGeometryChange()
        self.rect = QRectF(rect)
        self.update()

    def set_lut(self, lut: np.ndarray) -> None:
        """lut: (256, 3|4) uint8 RGB(A)."""
        lut = np.asarray(lut, dtype=np.uint32)
        self.lut = (0xFF000000 | (lut[:, 0] << 16) | (lut[:, 1] << 8) | lut[:, 2]).astype(np.uint32)

    def colorize(self, data: np.ndarray) -> np.ndarray:
        scale = 255.0 / max(1e-6, self.hi - self.lo)
        idx = np.clip((data - self.lo) * scale, 0, 255).astype(np.uint8)
        return self.lut[idx]

    def put_row(self, pos: int, row: np.ndarray) -> None:
        self.argb[pos] = self.colorize(row)
        self.pos = pos

    def recolor(self, wf: np.ndarray, pos: int) -> None:
        self.argb[:] = self.colorize(wf)
        self.pos = pos

    def boundingRect(self) -> QRectF:
        return QRectF(self.rect)

    def paint(self, p, *args) -> None:
        rows, bins = self.argb.shape
        r = self.rect
        # ungeglättet skalieren: einfacher Pixelzugriff statt (mehrfädigem) Glätten des Bildes
        p.setRenderHint(p.RenderHint.SmoothPixmapTransform, False)
        p.setRenderHint(p.RenderHint.Antialiasing, False)
        dy = r.height() / rows
        # oberer Teil: Ring ab pos (neueste Zeilen), unterer Teil: Ring 0..pos-1
        n1 = rows - self.pos
        p.drawImage(QRectF(r.x(), r.y(), r.width(), n1 * dy), self.qimg,
                    QRectF(0, self.pos, bins, n1))
        if self.pos:
            p.drawImage(QRectF(r.x(), r.y() + n1 * dy, r.width(), self.pos * dy), self.qimg,
                        QRectF(0, 0, bins, self.pos))


def ap_shape(center: float, width: float, level: float, n: int = 64) -> tuple[np.ndarray, np.ndarray]:
    half = width / 2.0
    x = np.linspace(center - half, center + half, n)
    u = (x - center) / half
    y = FLOOR + (level - FLOOR) * (1.0 - u ** 8)
    return x, y


class BandPanel(QWidget):
    apClicked = pyqtSignal(str)

    def __init__(self, band_key: str, parent=None):
        super().__init__(parent)
        self.band = BANDS[band_key]
        self.f0, self.f1 = self.band.f_min, self.band.f_max     # Bandgrenzen (Achse)
        self.s0, self.s1 = self.f0, self.f1                     # SDR-Sweep-Bereich (Raster)
        self.grid = self._make_grid()
        self.aps: list[AccessPoint] = []
        self.selected: str | None = None
        self.ap_items: dict[str, tuple[pg.PlotCurveItem, pg.TextItem]] = {}

        # SDR-Zustand
        self.offset = 0.0
        self.rows = 300
        self.wf = np.full((self.rows, GRID_BINS), -200.0, dtype=np.float32)
        self.wf_ts = np.zeros(self.rows)
        self.wf_pos = 0
        self.wf_count = 0
        self.live: np.ndarray | None = None
        self.peak: np.ndarray | None = None
        self.avg: np.ndarray | None = None
        self.show_peak = True
        self.show_avg = False
        self.show_live = True
        self.fill_on = False
        self.live_alpha = 1.0     # Glättung des Live-Spektrums (1 = aus)
        self.ap_alpha = 1.0       # Intensität der WLAN-Kanalkurven (0 = ausgeblendet)
        self.wf_dirty = False
        self.levels = (-100.0, -40.0)
        self._axis_t = 0.0
        self.mapper = GridMapper()
        # automatische Signalerkennung
        self.clf = SignalClassifier(band_key)
        self.detect_on = True
        self.show_marks = True
        self.mark_wlan = False
        self.row_abs = 0          # Anzahl bisher geschriebener Wasserfallzeilen
        self.clf_base = 0         # row_abs beim letzten Zurücksetzen des Klassifikators

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.info = QLabel(" ")
        self.info.setStyleSheet("color: #aaa; padding-left: 6px;")
        lay.addWidget(self.info)

        split = QSplitter(Qt.Orientation.Vertical)
        lay.addWidget(split, 1)

        # --- Kanalgraph -----------------------------------------------------
        self.cplot = pg.PlotWidget(axisItems={"bottom": ChannelAxis(band_key)})
        self.cplot.setLabel("left", "Signal", units="dBm")
        self.cplot.setLabel("bottom", f"Kanal ({self.band.name})")
        self.cplot.showGrid(x=True, y=True, alpha=0.2)
        self.cplot.setYRange(FLOOR, -15, padding=0)
        self.cplot.setXRange(self.f0, self.f1, padding=0)
        self.cplot.setLimits(xMin=self.f0 - 20, xMax=self.f1 + 20, yMin=-130, yMax=10)
        self.cplot.getViewBox().setMouseEnabled(x=True, y=False)
        self._add_band_markers(band_key)
        self.overlays_on: set[str] = set()
        self.overlay_items: dict[str, list] = {}
        self._build_overlays(band_key)

        # ohne Kantenglättung: bei 1500 Punkten und 25 Bildern/s deutlich günstiger
        self.c_avg = pg.PlotCurveItem(pen=pg.mkPen(CURVE_COLORS["avg"], width=1), antialias=False)
        self.c_peak = pg.PlotCurveItem(pen=pg.mkPen(CURVE_COLORS["peak"], width=1), antialias=False)
        self.c_live = pg.PlotCurveItem(pen=pg.mkPen(CURVE_COLORS["live"], width=1), antialias=False)
        # Füllung als eigene Kurve: Lücken (NaN) würden die Füllfläche der Linie verzerren
        self.c_fill = pg.PlotCurveItem(pen=pg.mkPen(None), fillLevel=FLOOR, antialias=False)
        self.c_fill.setZValue(4)
        self.cplot.addItem(self.c_fill)
        for c in (self.c_avg, self.c_peak, self.c_live):
            c.setZValue(5)
            self.cplot.addItem(c)
        self.set_curve_style(CURVE_COLORS, False, 0.5)
        self.sdr_span = SpansItem([(self.f0, self.f1)], brush=(80, 160, 255, 14),
                                  pen=pg.mkPen((80, 160, 255, 120), width=1))
        self.sdr_span.setZValue(-5)
        self.sdr_span.hide()
        self.cplot.addItem(self.sdr_span)
        split.addWidget(self.cplot)

        # --- Wasserfall -----------------------------------------------------
        self.taxis = TimeAxis(self)
        self.wplot = pg.PlotWidget(axisItems={"left": self.taxis})
        self.wplot.setLabel("bottom", "Frequenz", units="MHz")
        self.wplot.getAxis("bottom").enableAutoSIPrefix(False)
        self.wplot.setXLink(self.cplot)
        self.wplot.invertY(True)
        self.wplot.getViewBox().setMouseEnabled(x=True, y=False)
        self.wimg = WaterfallItem()
        self._place_image()
        self.wplot.addItem(self.wimg)
        self.wplot.setYRange(0, self.rows, padding=0)
        self.sel_region = SpansItem([(self.f0, self.f1)], y0=-1e5, y1=1e5,
                                    pen=pg.mkPen((255, 255, 255, 160), width=1, style=Qt.PenStyle.DashLine))
        self.sel_region.setZValue(10)
        self.sel_region.hide()
        self.wplot.addItem(self.sel_region)
        self.mark_items: dict[tuple[str, str], pg.ScatterPlotItem] = {}
        self.set_colormap("inferno")
        split.addWidget(self.wplot)
        split.setSizes([500, 400])

        # Synchronisierte Achsenbreite links, damit die X-Achsen übereinander liegen
        for p in (self.cplot, self.wplot):
            p.getAxis("left").setWidth(60)

        live = {self.c_live, self.c_peak, self.c_avg, self.c_fill}
        for it in self.cplot.getPlotItem().items:
            if it not in live:
                cache_item(it)
        for name in ("left", "bottom"):
            cache_item(self.cplot.getAxis(name))
            cache_item(self.wplot.getAxis(name))
        self.cplot.scene().sigMouseClicked.connect(self._on_click)
        self.cplot.scene().sigMouseMoved.connect(lambda pos: self._on_move(pos, self.cplot))
        self.wplot.scene().sigMouseMoved.connect(lambda pos: self._on_move(pos, self.wplot))

    # ------------------------------------------------------------------ Markierungen
    def _add_band_markers(self, band_key: str) -> None:
        if band_key == "5":
            spans = [(channel_to_freq(ch, "5") - 10, channel_to_freq(ch, "5") + 10)
                     for ch in sorted(DFS_CHANNELS_5G)]
            r = SpansItem(spans, brush=(255, 160, 0, 18))
            r.setZValue(-10)
            self.cplot.addItem(r)
            t = pg.TextItem("DFS", color=(255, 170, 40), anchor=(0.5, 0))
            t.setPos(channel_to_freq(100, "5") + 70, -16)
            self.cplot.addItem(t)
        if band_key == "6":
            fs = [channel_to_freq(ch, "6") for ch in band_channels("6") if is_psc(ch)]
            if fs:
                line = SpansItem([(f, f) for f in fs],
                                 pen=pg.mkPen((120, 200, 255, 60), style=Qt.PenStyle.DotLine))
                line.setZValue(-10)
                self.cplot.addItem(line)

    # ------------------------------------------------------------------ andere Funkdienste
    def _build_overlays(self, band_key: str) -> None:
        """Alle Überlagerungen des Bandes einmal anlegen (zunächst unsichtbar)."""
        font = QFont()
        font.setPointSize(8)
        label_row = 0
        raster_row = 0
        for o in ovl.overlays_for_band(band_key):
            items = []
            r, g, b = o.color
            if o.style == "raster":
                # Strichleiste am unteren Rand: ein Strich pro Kanal
                y0 = FLOOR + 0.5 + raster_row * 3.0
                bars = pg.BarGraphItem(x=[c.center for c in o.channels], height=2.2, y0=y0,
                                       width=[c.width * 0.7 for c in o.channels],
                                       brush=(r, g, b, 190), pen=pg.mkPen(None))
                bars.setZValue(6)
                items.append(bars)
                raster_row += 1
            else:
                y = -19.0 - label_row * 4.0
                # nur Kanäle, deren Mitte innerhalb der Bandachse liegt
                chans = [c for c in o.channels if self.f0 <= c.center <= self.f1]
                if not chans:
                    continue
                if o.group_label:
                    lo = max(self.f0, min(c.center - c.width / 2 for c in chans))
                    hi = min(self.f1, max(c.center + c.width / 2 for c in chans))
                    t = pg.TextItem(o.group_label, color=(r, g, b), anchor=(0.5, 0))
                    t.setFont(font)
                    t.setPos((lo + hi) / 2, y)
                    t.setZValue(7)
                    items.append(t)
                reg = SpansItem([(c.center - c.width / 2, c.center + c.width / 2) for c in chans],
                                brush=(r, g, b, 26), pen=pg.mkPen((r, g, b, 120), width=1))
                reg.setZValue(-8)
                items.append(reg)
                for c in chans:
                    if c.label and not o.group_label:
                        t = pg.TextItem(o.tag + c.label, color=(r, g, b), anchor=(0.5, 0))
                        t.setFont(font)
                        t.setPos(c.center, y)
                        t.setZValue(7)
                        items.append(t)
                label_row += 1
            for it in items:
                it.setToolTip(o.hint)
                it.hide()
                self.cplot.addItem(it)
            self.overlay_items[o.key] = items

    def set_overlays(self, enabled) -> None:
        self.overlays_on = set(enabled)
        for key, items in self.overlay_items.items():
            vis = key in self.overlays_on
            for it in items:
                it.setVisible(vis)

    # ------------------------------------------------------------------ Darstellung
    def set_curve_style(self, colors: dict, fill: bool, fill_alpha: float) -> None:
        """Farben der Kurven (live/peak/avg, QColor oder RGBA) und Füllung unter dem Live-Spektrum."""
        for key, curve in (("live", self.c_live), ("peak", self.c_peak), ("avg", self.c_avg)):
            curve.setPen(pg.mkPen(colors[key], width=1))
        self.fill_on = fill
        col = pg.mkColor(colors["live"])
        col.setAlpha(255)
        self.c_fill.setBrush(fill_brush(col, fill_alpha))
        self.wf_dirty = True

    def set_live_averaging(self, pct: float) -> None:
        """0 % = ungeglättet, 100 % = Mittel über ca. 10 Sweeps."""
        self.live_alpha = 1.0 / (1.0 + max(0.0, min(100.0, pct)) / 10.0)

    def set_ap_intensity(self, f: float) -> None:
        """Deckkraft der WLAN-Kanalkurven: 1 = volle Stärke, 0 = ausgeblendet."""
        f = max(0.0, min(1.0, f))
        if f != self.ap_alpha:
            self.ap_alpha = f
            self.set_selected(self.selected)

    # ------------------------------------------------------------------ APs
    def set_aps(self, aps: list[AccessPoint], all_aps: list[AccessPoint] | None = None) -> None:
        """aps: angezeigte APs; all_aps: alle (für die Signalerkennung, unabhängig vom Filter)."""
        self.aps = [ap for ap in aps if ap.band == self.band.key]
        self.clf.set_aps([ap for ap in (aps if all_aps is None else all_aps) if ap.band == self.band.key])
        seen = set()
        for ap in self.aps:
            seen.add(ap.bssid)
            item = self.ap_items.get(ap.bssid)
            if item is None:
                col = ap_color(ap.bssid)
                curve = pg.PlotCurveItem(pen=pg.mkPen(col, width=2), fillLevel=FLOOR)
                text = pg.TextItem(color=col, anchor=(0.5, 1.0))
                self.cplot.addItem(curve)
                self.cplot.addItem(text)
                cache_item(curve)
                cache_item(text)
                item = (curve, text)
                self.ap_items[ap.bssid] = item
            curve, text = item
            x, y = ap_shape(ap.center_freq, ap.width, ap.signal)
            curve.setData(x, y)
            text.setText(ap.display_ssid)
            text.setPos(ap.center_freq, ap.signal)
        for bssid in list(self.ap_items):
            if bssid not in seen:
                curve, text = self.ap_items.pop(bssid)
                self.cplot.removeItem(curve)
                self.cplot.removeItem(text)
        self.set_selected(self.selected)

    def set_selected(self, bssid: str | None) -> None:
        self.selected = bssid
        k = self.ap_alpha
        for b, (curve, text) in self.ap_items.items():
            sel = b == bssid
            vis = k > 0
            curve.setVisible(vis)
            text.setVisible(vis)
            if not vis:
                continue
            col = QColor(ap_color(b))
            line = QColor(col)
            line.setAlphaF(k)
            curve.setPen(pg.mkPen(line, width=4 if sel else 2))
            fill = QColor(col)
            fill.setAlpha(round((110 if sel else (25 if bssid else 45)) * k))
            curve.setBrush(fill)
            text.setColor(line)
            curve.setZValue(3 if sel else 1)
            text.setZValue(4 if sel else 2)
        ap = next((a for a in self.aps if a.bssid == bssid), None)
        if ap is not None:
            self.sel_region.setRegion((ap.center_freq - ap.width / 2, ap.center_freq + ap.width / 2))
            self.sel_region.show()
        else:
            self.sel_region.hide()

    def _on_click(self, ev) -> None:
        vb = self.cplot.getViewBox()
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        p = vb.mapSceneToView(ev.scenePos())
        hits = [ap for ap in self.aps
                if abs(p.x() - ap.center_freq) <= ap.width / 2 and p.y() <= ap.signal]
        if hits:
            # das "oberste" (am knappsten getroffene) AP gewinnt
            ap = min(hits, key=lambda a: a.signal - p.y())
            self.apClicked.emit(ap.bssid)

    def _on_move(self, pos, plot) -> None:
        vb = plot.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            return
        p = vb.mapSceneToView(pos)
        f = p.x()
        if not (self.f0 <= f <= self.f1):
            return
        ch = freq_to_channel(f)
        txt = f"f = {f:.1f} MHz  (Kanal ≈ {ch})"
        other = ovl.describe(f, self.band.key, self.overlays_on)
        if other:
            txt += "  [" + ", ".join(other) + "]"
        i = int((f - self.s0) / (self.s1 - self.s0) * GRID_BINS)
        in_sdr = 0 <= i < GRID_BINS
        i = min(max(i, 0), GRID_BINS - 1)
        if plot is self.wplot:
            row = int(p.y())
            if in_sdr and 0 <= row < self.rows and row < self.wf_count:
                v = self.wf[(self.wf_pos + row) % self.rows, i]
                age = self.age_of_row(row)
                txt += f"   |   Wasserfall: {v + self.offset:.1f} dB vor {age:.1f}s"
                b = self._mark_at(f, p.y()) if self.detect_on and self.show_marks else None
                if b is not None:
                    txt += (f"   |   erkannt: {b.label} – {b.center:.1f} MHz, {b.width:.1f} MHz breit, "
                            f"{format_duration(b.duration)}, {b.peak + self.offset:.0f} dB")
        else:
            txt += f"   |   Cursor: {p.y():.0f} dBm"
            if in_sdr and self.live is not None and np.isfinite(self.live[i]):
                txt += f"   |   SDR: {self.live[i] + self.offset:.1f} dB"
            if in_sdr and self.peak is not None and np.isfinite(self.peak[i]):
                txt += f" (Peak {self.peak[i] + self.offset:.1f})"
        self.info.setText(txt)

    # ------------------------------------------------------------------ SDR
    def _make_grid(self) -> np.ndarray:
        return self.s0 + (np.arange(GRID_BINS) + 0.5) * (self.s1 - self.s0) / GRID_BINS

    def set_sdr_range(self, lo: float, hi: float, zoom: bool = True) -> None:
        """Sweep-Bereich setzen: Raster (Auflösung) und Wasserfall folgen ihm."""
        lo, hi = max(lo, self.f0), min(hi, self.f1)
        if hi <= lo:
            lo, hi = self.f0, self.f1
        changed = (lo, hi) != (self.s0, self.s1)
        self.s0, self.s1 = lo, hi
        self.grid = self._make_grid()
        if changed:
            self.reset_sdr()
            self._place_image()
        self.sdr_span.setRegion((lo, hi))
        self.sdr_span.setVisible(hi - lo < (self.f1 - self.f0) - 0.5)
        if zoom:
            m = max(2.0, (hi - lo) * 0.05)
            self.cplot.setXRange(max(self.f0 - 20, lo - m), min(self.f1 + 20, hi + m), padding=0)

    def view_range(self) -> tuple[float, float]:
        lo, hi = self.cplot.getViewBox().viewRange()[0]
        return max(lo, self.f0), min(hi, self.f1)

    def reset_sdr(self) -> None:
        self.reset_detection()
        self.wf[:] = -200.0
        self.wf_ts[:] = 0
        self.wf_pos = 0
        self.wf_count = 0
        self.wimg.recolor(self.wf, 0)
        self.live = self.peak = self.avg = None
        for c in (self.c_live, self.c_peak, self.c_avg, self.c_fill):
            c.setData([], [])
        self.wf_dirty = True

    def reset_peak(self) -> None:
        self.peak = None if self.live is None else self.live.copy()

    def set_rows(self, rows: int) -> None:
        if rows == self.rows:
            return
        self.rows = rows
        self.wf = np.full((rows, GRID_BINS), -200.0, dtype=np.float32)
        self.wf_ts = np.zeros(rows)
        self.wf_pos = self.wf_count = 0
        self._place_image()
        self.wplot.setYRange(0, rows, padding=0)
        self.wf_dirty = True

    def push_sweep(self, freqs: np.ndarray, power: np.ndarray, ts: float) -> None:
        self.push_sweeps([(freqs, power)], ts)

    def push_sweeps(self, sweeps: list, ts: float) -> None:
        """Mehrere Sweeps als eine Wasserfallzeile (Maximum: kurze Bursts bleiben sichtbar)."""
        row = self.mapper.map(sweeps, self.s0, self.s1, GRID_BINS)
        if row is None:
            return
        a = self.live_alpha
        if a >= 1.0 or self.live is None or self.live.shape != row.shape:
            self.live = row
        else:     # exponentielles Mittel in dB (wie gqrx); Lücken übernehmen den neuen Wert
            self.live = np.where(np.isfinite(self.live), self.live + a * (row - self.live), row)
        if self.peak is None:
            self.peak = row.copy()
        else:
            self.peak = np.fmax(self.peak, row)
        if self.avg is None:
            self.avg = row.copy()
        else:
            self.avg = np.where(np.isfinite(self.avg), 0.9 * self.avg + 0.1 * row, row)
        self.wf_pos = (self.wf_pos - 1) % self.rows       # neueste Zeile bei wf_pos
        self.wf[self.wf_pos] = np.nan_to_num(row, nan=-200.0)
        self.wf_ts[self.wf_pos] = ts
        self.wimg.put_row(self.wf_pos, self.wf[self.wf_pos])
        self.wf_count = min(self.wf_count + 1, self.rows)
        self.wf_dirty = True
        if self.detect_on:
            self.clf.feed(self.grid, row, ts)
        self.row_abs += 1

    # ------------------------------------------------------------------ Signalerkennung
    def reset_detection(self) -> None:
        self.clf.reset()
        self.clf_base = self.row_abs
        self._clear_marks()

    def set_detection(self, on: bool, threshold: float, marks: bool, mark_wlan: bool) -> None:
        if on != self.detect_on or threshold != self.clf.threshold:
            self.reset_detection()
        self.detect_on = on
        self.clf.threshold = threshold
        self.show_marks = marks
        self.mark_wlan = mark_wlan
        self.wf_dirty = True

    def _mark_y(self, b) -> float:
        """Wasserfallzeile (0 = neueste) einer Markierung."""
        return self.row_abs - 1 - (self.clf_base + b.row_end) + 0.5

    def _visible_marks(self):
        for b in self.clf.marks:
            if b.cls == "wlan" and not self.mark_wlan:
                continue
            y = self._mark_y(b)
            if 0 <= y < min(self.rows, self.wf_count):
                yield b, y

    def _clear_marks(self) -> None:
        for it in self.mark_items.values():
            it.setData([], [])

    def _mark_item(self, cls: str, sym: str) -> pg.ScatterPlotItem:
        it = self.mark_items.get((cls, sym))
        if it is None:
            r, g, b = CLASSES[cls].color
            # einheitliches Symbol/Farbe je Item -> pyqtgraph zeichnet aus dem Symbol-Cache
            it = pg.ScatterPlotItem(size=7, pxMode=True, symbol=sym, brush=pg.mkBrush(r, g, b, 210),
                                    pen=pg.mkPen(0, 0, 0, 160), antialias=False)
            it.setZValue(12)
            self.wplot.addItem(it)
            self.mark_items[(cls, sym)] = it
        return it

    def _update_marks(self) -> None:
        if not (self.detect_on and self.show_marks):
            self._clear_marks()
            return
        groups: dict[tuple[str, str], tuple[list, list]] = {}
        for b, y in self._visible_marks():
            xs, ys = groups.setdefault((b.cls, "o" if b.width <= 4.5 else "s"), ([], []))
            xs.append(b.center)
            ys.append(y)
        for key in set(groups) | set(self.mark_items):
            xs, ys = groups.get(key, ((), ()))
            it = self._mark_item(*key)
            if len(xs) or len(it.data):
                it.setData(np.asarray(xs, float), np.asarray(ys, float))

    def _mark_at(self, freq: float, y: float):
        tol_f = max(0.5, (self.s1 - self.s0) * 0.006)
        best = None
        for b, my in self._visible_marks():
            if abs(my - y) <= 2.0 and b.lo - tol_f <= freq <= b.hi + tol_f:
                d = abs(freq - b.center) + abs(my - y)
                if best is None or d < best[0]:
                    best = (d, b)
        return None if best is None else best[1]

    def _place_image(self) -> None:
        lo, hi = self.levels
        self.wimg.alloc(self.rows, GRID_BINS)
        self.wimg.lo, self.wimg.hi = lo - self.offset, hi - self.offset
        self.wimg.recolor(self.wf, self.wf_pos)
        self.wimg.set_rect(QRectF(self.s0, 0, self.s1 - self.s0, self.rows))

    def age_of_row(self, row: float) -> float | None:
        r = int(round(row))
        if r < 0 or r >= self.wf_count:
            return None
        return time.time() - self.wf_ts[(self.wf_pos + r) % self.rows]

    def set_colormap(self, name: str) -> None:
        try:
            cmap = pg.colormap.get(name)
        except Exception:  # noqa: BLE001
            cmap = pg.colormap.get("viridis")
        self.wimg.set_lut(cmap.getLookupTable(nPts=256, alpha=False))
        self.wimg.recolor(self.wf, self.wf_pos)
        self.wimg.update()

    def set_levels(self, lo: float, hi: float) -> None:
        self.levels = (lo, hi)
        self.wf_dirty = True

    def auto_levels(self) -> tuple[float, float] | None:
        # Ringpuffer: die gefüllten Zeilen liegen ab wf_pos (neueste) und laufen ggf. über das Ende
        if self.wf_count < self.rows:
            data = self.wf[(self.wf_pos + np.arange(self.wf_count)) % self.rows]
        else:
            data = self.wf
        valid = data[data > -199]
        if valid.size < 10:
            return None
        # Untergrenze knapp unter dem Rauschboden (Median), Obergrenze an den stärksten Signalen
        lo = float(np.percentile(valid, 50)) + self.offset - 3
        hi = float(np.percentile(valid, 99.9)) + self.offset + 2
        return lo, max(hi, lo + 15)

    def refresh(self) -> None:
        """Anzeige aktualisieren (vom GUI-Timer aufgerufen)."""
        if not self.wf_dirty:
            return
        self.wf_dirty = False
        now = time.monotonic()
        if now - self._axis_t > 0.5:   # Zeitachse (Alter) regelmäßig neu beschriften
            self._axis_t = now
            self.taxis.picture = None
            self.taxis.update()
        lo, hi = self.levels[0] - self.offset, self.levels[1] - self.offset
        if (lo, hi) != (self.wimg.lo, self.wimg.hi):
            self.wimg.lo, self.wimg.hi = lo, hi
            self.wimg.recolor(self.wf, self.wf_pos)
        self.wimg.update()
        self._update_marks()
        x = self.grid
        for curve, data, show in ((self.c_live, self.live, self.show_live),
                                  (self.c_peak, self.peak, self.show_peak),
                                  (self.c_avg, self.avg, self.show_avg)):
            if show and data is not None:
                curve.setData(x, data + self.offset, connect="finite")
            else:
                curve.setData([], [])
        if self.fill_on and self.show_live and self.live is not None:
            y = self.live + self.offset
            self.c_fill.setData(x, np.maximum(np.nan_to_num(y, nan=FLOOR), FLOOR))
        else:
            self.c_fill.setData([], [])
