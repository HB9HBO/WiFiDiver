"""Signalverlauf (rollierendes Zeitfenster) pro BSSID."""

from __future__ import annotations

import collections
import time

import numpy as np
import pyqtgraph as pg

from ..wifi.model import AccessPoint
from .ap_table import ap_color


class HistoryWidget(pg.PlotWidget):
    def __init__(self, window_s: float = 120.0, parent=None):
        super().__init__(parent)
        self.window_s = window_s
        self.data: dict[str, collections.deque] = {}
        self.names: dict[str, str] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.selected: str | None = None
        self.band_filter: str | None = None
        self.bands: dict[str, str | None] = {}
        self.ssids: dict[str, str] = {}
        self.accept_ssid = lambda ssid: True
        self.setLabel("left", "Signal", units="dBm")
        self.setLabel("bottom", "Zeit (s, relativ zu jetzt)")
        self.showGrid(x=True, y=True, alpha=0.2)
        self.setYRange(-100, -20)
        self.setXRange(-window_s, 0)
        self.legend = self.addLegend(offset=(10, 10), labelTextSize="8pt")

    def add_scan(self, aps: list[AccessPoint]) -> None:
        now = time.time()
        for ap in aps:
            dq = self.data.setdefault(ap.bssid, collections.deque())
            dq.append((now, ap.signal))
            self.names[ap.bssid] = ap.display_ssid
            self.bands[ap.bssid] = ap.band
            self.ssids[ap.bssid] = ap.ssid
        for bssid, dq in list(self.data.items()):
            while dq and dq[0][0] < now - self.window_s:
                dq.popleft()
            if not dq:
                del self.data[bssid]
        self.redraw()

    def set_selected(self, bssid: str | None) -> None:
        self.selected = bssid
        self.redraw()

    def redraw(self) -> None:
        now = time.time()
        wanted = {b for b in self.data
                  if (self.band_filter is None or self.bands.get(b) == self.band_filter)
                  and self.accept_ssid(self.ssids.get(b, ""))}
        for bssid in list(self.curves):
            if bssid not in wanted:
                self.removeItem(self.curves.pop(bssid))
                self.legend.removeItem(f"{self.names.get(bssid, '')} [{bssid[-5:]}]")
        for bssid in wanted:
            arr = np.array(self.data[bssid])
            col = ap_color(bssid)
            sel = bssid == self.selected
            pen = pg.mkPen(col, width=3.5 if sel else 1.5)
            if bssid not in self.curves:
                c = self.plot(name=f"{self.names[bssid]} [{bssid[-5:]}]", pen=pen,
                              symbol="o", symbolSize=4, symbolBrush=col, symbolPen=None)
                self.curves[bssid] = c
            c = self.curves[bssid]
            c.setPen(pen)
            c.setZValue(10 if sel else 1)
            c.setData(arr[:, 0] - now, arr[:, 1])
