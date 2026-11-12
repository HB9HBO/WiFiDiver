"""Simuliertes SDR: Rauschen, WLAN-Bursts der bekannten APs und typische Fremdsignale
(Bluetooth-Hopping, BLE-Advertising, Zigbee, Mikrowelle, FPV-Video, Radar-Pulse)."""

from __future__ import annotations

import random
import time
from typing import Callable

import numpy as np

from .base import Param, SdrBackend, Sweep


class DemoBackend(SdrBackend):
    name = "Demo"
    f_min_mhz = 1.0
    f_max_mhz = 7250.0
    params = [
        Param("rate", "Sweeps pro Sekunde", "float", 20.0, 1.0, 100.0, 1.0, live=True),
        Param("noise", "Rauschboden", "float", -95.0, -130.0, -40.0, 1.0, live=True, suffix=" dB"),
        Param("others", "Fremdsignale simulieren", "bool", True, live=True),
    ]

    def __init__(self, settings: dict | None = None, ap_source: Callable[[], list] | None = None):
        super().__init__(settings)
        self.ap_source = ap_source or (lambda: [])

    def run(self) -> None:
        lo, hi = self.range
        n = int(min(4000, max(500, (hi - lo) * 10)))
        f = np.linspace(lo, hi, n)
        while not self.stopped:
            t0 = time.time()
            self.take_pending()
            p = self.settings["noise"] + np.random.normal(0, 1.5, n)
            for ap in self.ap_source():
                duty = 0.15 + (ap.utilization or 20) / 150.0
                if random.random() > duty:
                    continue
                c, w = ap.center_freq, ap.width
                if c + w / 2 < lo or c - w / 2 > hi:
                    continue
                lvl = ap.signal + random.gauss(0, 2)
                edge = np.abs(f - c) - (w / 2 - 1.0)
                shape = np.where(edge <= 0, 0.0, -edge * 10.0)  # 10 dB/MHz Flanke (OFDM)
                p = np.maximum(p, lvl + np.maximum(shape, -60) + np.random.normal(0, 1, n))
            if self.settings["others"]:
                self._others(f, p, t0)
            self._emit(Sweep(f.copy(), p.astype(np.float32), lo, hi, time.time(), time.time() - t0))
            dt = 1.0 / float(self.settings["rate"]) - (time.time() - t0)
            if dt > 0:
                self._stop.wait(dt)

    @staticmethod
    def _burst(f: np.ndarray, p: np.ndarray, fc: float, half: float, lvl: float) -> None:
        """Schmaler Burst mit Flanke (20 dB/MHz) in das Spektrum eintragen."""
        d = np.abs(f - fc) - half
        m = d < 2.0
        if m.any():
            shape = lvl - np.maximum(d[m], 0.0) * 20.0 + np.random.normal(0, 1, int(m.sum()))
            p[m] = np.maximum(p[m], shape)

    def _others(self, f: np.ndarray, p: np.ndarray, t: float) -> None:
        lo, hi = f[0], f[-1]
        if lo < 2481 and hi > 2401:
            for _ in range(3):                                   # Bluetooth: 1-MHz-Hops
                self._burst(f, p, 2402 + random.randint(0, 78), 0.4, -70 + random.gauss(0, 5))
            if random.random() < 0.15:                           # BLE-Advertising 37/38/39
                for fc in (2402, 2426, 2480):
                    if random.random() < 0.8:
                        self._burst(f, p, fc, 0.8, -66 + random.gauss(0, 3))
            for fc, prob in ((2450, 0.20), (2475, 0.12)):         # Zigbee Kanal 20 und 25
                if random.random() < prob:
                    self._burst(f, p, fc, 1.0, -74 + random.gauss(0, 2))
            if (t % 40) < 12 and random.random() < 0.6:          # Mikrowelle: 12 s von 40 s
                fc = 2455 + random.gauss(0, 4)
                w = random.uniform(8, 30)
                m = np.abs(f - fc) < w / 2
                p[m] = np.maximum(p[m], -55 - np.abs(f[m] - fc) * random.uniform(1.0, 2.0)
                                  + np.random.normal(0, 2, int(m.sum())))
        if lo < 5671 and hi > 5645:                              # FPV-Sender auf R1 (5658 MHz)
            m = np.abs(f - 5658) < 9
            p[m] = np.maximum(p[m], -78 - np.abs(f[m] - 5658) * 0.6 + np.random.normal(0, 1, int(m.sum())))
        if lo < 5625 and hi > 5615 and random.random() < 0.12:   # Wetterradar-Pulse
            self._burst(f, p, 5620.0, 0.5, -72 + random.gauss(0, 3))
