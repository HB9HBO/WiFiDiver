"""Automatische Einordnung von Bursts im Wasserfall (nur WLAN-Bänder).

Keine Dekodierung: Jede Wasserfallzeile wird gegen einen gleitenden Rauschboden
geprüft, zusammenhängende Bereiche über der Schwelle werden über aufeinanderfolgende
Zeilen zu *Bursts* verfolgt. Ein Burst wird anhand von

* Frequenzraster (BLE-Advertising, 802.15.4, WLAN-Kanäle, FPV, ITS-G5),
* Breite (schmal ≈ 1–3 MHz, WLAN ≥ 20 MHz),
* Dauer (kurz, andauernd) und
* Wiederholung im Zeitfenster (Frequenzsprung über viele Kanäle oder
  gehäuft auf einem Kanal)

einer Klasse zugeordnet. Ein Sweep-SDR sieht kurze Bursts nur, wenn es gerade auf
deren Frequenz steht – die Auswertung ist daher statistisch.
"""

from __future__ import annotations

import collections
import math
import warnings
from dataclasses import dataclass, field

import numpy as np

from ..wifi.overlays import BLE_ADV, OVERLAY_BY_KEY, zigbee_channel_freq

# ---------------------------------------------------------------- Klassen


@dataclass(frozen=True)
class SignalClass:
    key: str
    name: str
    color: tuple[int, int, int]
    hint: str


def _ovl_color(key: str) -> tuple[int, int, int]:
    return OVERLAY_BY_KEY[key].color


CLASSES: dict[str, SignalClass] = {c.key: c for c in (
    SignalClass("wlan", "WLAN", (140, 140, 170),
                "Flaches OFDM-Spektrum im Kanal eines bekannten APs bzw. auf dem WLAN-Raster "
                "(20/40/80/160 MHz). Sweep-SDRs erfassen kurze Frames oft nur teilweise "
                "(HackRF: 5-MHz-Streifen) – solche Teilstücke werden ebenfalls zugeordnet."),
    SignalClass("ble_adv", "BLE Advertising", _ovl_color("ble_adv"),
                "Schmale Bursts gehäuft auf 2402/2426/2480 MHz (BLE-Kanäle 37/38/39)"),
    SignalClass("zigbee", "Zigbee / Thread", _ovl_color("zigbee"),
                "Schmale Bursts gehäuft auf einem 802.15.4-Kanal (2405 + 5·n MHz), ohne Frequenzsprung"),
    SignalClass("bt", "Bluetooth (Frequenzsprung)", _ovl_color("bt_classic"),
                "Viele schmale Bursts (1–2 MHz) auf wechselnden, jeweils selten belegten "
                "Frequenzen: Bluetooth Classic oder BLE-Verbindungen. Gezählt wird nur außerhalb "
                "aktiver WLAN-Kanäle (dort wären schmale Stücke mehrdeutig)."),
    SignalClass("microwave", "Mikrowellenherd", _ovl_color("microwave"),
                "Breite, unregelmäßig geformte Signale um 2440–2480 MHz, die nicht auf ein "
                "WLAN-Kanalraster passen und sich wiederholen"),
    SignalClass("fpv", "FPV-Video (analog)", _ovl_color("fpv"),
                "Andauerndes 8–30 MHz breites Signal auf einer 5,8-GHz-FPV-Frequenz"),
    SignalClass("its_g5", "ITS-G5 / Car2X", _ovl_color("its_g5"),
                "≈ 10 MHz breite Bursts auf einem ITS-G5-Kanal (5860–5920 MHz)"),
    SignalClass("radar", "Radar-Pulse", _ovl_color("weather_radar"),
                "Sehr kurze, schmale Pulse, wiederholt auf derselben Frequenz im DFS-Bereich"),
    SignalClass("carrier", "Dauerträger / Störlinie", (220, 190, 0),
                "Schmale Linie, die ununterbrochen anliegt. Meist eine Störlinie des SDR selbst "
                "(HackRF: oft auf runden Frequenzen), seltener ein echter Dauersender (Funkkamera, "
                "defektes Netzteil). Test: bleibt die Linie ohne Antenne bestehen, stammt sie "
                "aus dem SDR."),
    SignalClass("narrow", "schmalbandig, unbekannt", (160, 160, 160),
                "Schmaler Burst ohne eindeutiges Muster, oder immer wieder auf derselben "
                "Frequenz (kein Frequenzsprung, kein bekanntes Raster)"),
    SignalClass("wide", "breitbandig, unbekannt", (130, 130, 130),
                "Breiter Burst, der auf kein bekanntes Raster passt"),
)}

ORDER = list(CLASSES)

# ---------------------------------------------------------------- Raster

BT_LO, BT_HI = 2402, 2480
NARROW = 3.5           # MHz: bis hierher "schmal" (BT/BLE/802.15.4); breiter + flach = OFDM
ZIGBEE = {c: zigbee_channel_freq(c) for c in range(11, 27)}
ADV_BY_FREQ = {f: c for c, f in BLE_ADV.items()}

# 5,8-GHz-FPV-Frequenzen (Bänder A, B, E, F, R)
FPV_BANDS = {
    "A": (5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725),
    "B": (5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866),
    "E": (5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945),
    "F": (5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880),
    "R": (5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917),
}


def _wlan_centers() -> dict[str, dict[int, list[float]]]:
    """Mittenfrequenzen der Standardkanäle je Band und Breite."""
    c24 = {20: [2407.0 + 5 * c for c in range(1, 14)] + [2484.0],
           40: [2407.0 + 5 * c for c in range(3, 12)]}
    ch5 = {20: list(range(36, 65, 4)) + list(range(100, 145, 4)) + list(range(149, 178, 4)),
           40: [38, 46, 54, 62, 102, 110, 118, 126, 134, 142, 151, 159, 167, 175],
           80: [42, 58, 106, 122, 138, 155, 171],
           160: [50, 114, 163]}
    c5 = {w: [5000.0 + 5 * c for c in chs] for w, chs in ch5.items()}
    c6 = {w: [5950.0 + 5 * c for c in range(1 + 2 * (w // 20 - 1), 234, 4 * (w // 20))]
          for w in (20, 40, 80, 160)}
    return {"2.4": c24, "5": c5, "6": c6}


WLAN_CENTERS = _wlan_centers()


def fpv_channel(freq: float, tol: float = 4.0) -> str | None:
    best = None
    for band, freqs in FPV_BANDS.items():
        for i, f in enumerate(freqs):
            d = abs(freq - f)
            if d <= tol and (best is None or d < best[0]):
                best = (d, f"{band}{i + 1}")
    return None if best is None else best[1]


def its_channel(freq: float, tol: float = 2.0) -> int | None:
    for k in range(7):
        if abs(freq - (5860.0 + 10 * k)) <= tol:
            return 172 + 2 * k
    return None


def wlan_match(band: str, center: float, width: float) -> int | None:
    """Kanalbreite (20/40/80/160), wenn Mitte und Breite auf ein WLAN-Raster passen."""
    for w, centers in WLAN_CENTERS.get(band, {}).items():
        if not 0.6 * w <= width <= 1.35 * w:
            continue
        tol = max(3.0, 0.08 * w)
        if any(abs(center - c) <= tol for c in centers):
            return w
    return None


# ---------------------------------------------------------------- Datenstrukturen


@dataclass
class Burst:
    center: float           # MHz
    width: float            # MHz (Kern: bis 10 dB unter der Spitze)
    lo: float
    hi: float
    peak: float             # dB (Rohwert ohne Offset)
    t0: float
    t1: float
    rows: int
    row_end: int            # absolute Zeilennummer der letzten Zeile
    duration: float = 0.0   # s (Auflösung = Zeilenabstand)
    flat: float = 1.0       # Anteil des Kerns ≤ 6 dB unter der Spitze
    wlan_ctx: str | None = None   # neben einem WLAN-Stück im selben Kanal (SSID bzw. "")
    counted: bool = False   # in self.narrow gezählt
    cls: str = ""
    detail: str = ""        # z.B. "38", "Z15", "R3", SSID

    @property
    def label(self) -> str:
        c = CLASSES.get(self.cls)
        name = c.name if c else self.cls
        return f"{name} {self.detail}".strip()


@dataclass
class Finding:
    key: str
    name: str
    color: tuple[int, int, int]
    hint: str
    count: int
    rate: float              # pro Sekunde
    where: str               # Kanäle/Frequenzen
    level: float             # dB (Rohwert ohne Offset)
    last_seen: float
    active: bool = False     # andauerndes Signal gerade aktiv


@dataclass
class _Track:
    lo: float
    hi: float
    center: float
    width: float
    peak: float
    t0: float
    t1: float
    row_start: int
    row_last: int
    rows: int = 1
    centers: list = field(default_factory=list)
    flat: float = 1.0
    wlan_ctx: str | None = None


# ---------------------------------------------------------------- Klassifikator


class SignalClassifier:
    """Erkennt und klassifiziert Bursts in aufeinanderfolgenden Spektrumszeilen."""

    HIST_ROWS = 64          # Zeilen für den Rauschboden (Median je Bin)
    FLOOR_EVERY = 8         # Rauschboden alle n Zeilen neu schätzen
    MAX_SEGMENTS = 200
    MIN_WIDTH = 0.25        # MHz: schmaler ist Rauschen einzelner FFT-Bins

    def __init__(self, band: str, threshold: float = 10.0, window: float = 5.0):
        self.band = band
        self.threshold = threshold
        self.window = window
        self.aps: list[tuple[float, float, str]] = []   # (Mitte, Breite, SSID)
        self.reset()

    # -- Zustand ----------------------------------------------------------
    def reset(self) -> None:
        self.grid: np.ndarray | None = None
        self.hist: np.ndarray | None = None
        self.hist_n = 0
        self.floor: np.ndarray | None = None
        self.spur: np.ndarray | None = None
        self.base = float("nan")      # typischer Rauschboden (Median über alle Bins)
        self.steady: list[Burst] = []  # Dauersignale (stecken im Rauschboden selbst)
        self._steady_since: dict[float, float] = {}
        self._floor_ts = 0.0
        self.row_index = 0
        self.row_dt = 0.0
        self._last_ts: float | None = None
        self.tracks: list[_Track] = []
        self.recent: collections.deque[Burst] = collections.deque()   # Zeitfenster
        self.narrow = collections.Counter()     # schmale Bursts außerhalb von WLAN-Kanälen je MHz
        self.wlan_spans: list[tuple[float, float]] = []
        self.marks: collections.deque[Burst] = collections.deque(maxlen=600)

    def set_aps(self, aps) -> None:
        """Bekannte APs; APs auf demselben Kanal werden zusammengefasst – ohne Dekodierung
        lässt sich ein Frame keinem einzelnen von ihnen zuordnen (stärkster zuerst)."""
        groups: dict[tuple[float, float], list] = {}
        for a in sorted(aps, key=lambda a: -a.signal):
            names = groups.setdefault((a.center_freq, float(a.width)), [])
            if a.display_ssid not in names:
                names.append(a.display_ssid)
        self.aps = [(c, w, "/".join(n[:3]) + (f" +{len(n) - 3}" if len(n) > 3 else ""))
                    for (c, w), n in groups.items()]
        self._update_wlan_spans()

    # -- Eingang ------------------------------------------------------------
    def feed(self, grid: np.ndarray, row: np.ndarray, ts: float) -> list[Burst]:
        """Eine Spektrumszeile (Raster `grid`, Leistung `row` in dB, NaN = kein Wert)."""
        if self.grid is None or len(grid) != len(self.grid) or grid[0] != self.grid[0] \
                or grid[-1] != self.grid[-1]:
            self.reset()
            self.grid = grid.copy()
            self.hist = np.full((self.HIST_ROWS, len(grid)), np.nan, dtype=np.float32)
        if self._last_ts is not None:
            dt = max(ts - self._last_ts, 1e-4)
            self.row_dt = dt if self.row_dt == 0 else 0.9 * self.row_dt + 0.1 * dt
        self._last_ts = ts

        self.hist[self.row_index % self.HIST_ROWS] = row
        self.hist_n = min(self.hist_n + 1, self.HIST_ROWS)
        if self.floor is None or self.row_index % self.FLOOR_EVERY == 0:
            self._update_floor(row, ts)
        idx = self.row_index
        self.row_index += 1
        if self.hist_n < 8:          # Rauschboden (und Störlinien) erst einschwingen lassen
            return []

        segs = self._segments(row)
        closed = self._track(segs, ts, idx)
        self._expire(ts)
        if closed:
            self._update_wlan_spans()
        for b in closed:
            self._classify(b)
            self.recent.append(b)
            if b.width <= NARROW and b.cls != "wlan":
                self.narrow[int(round(b.center))] += 1
                b.counted = True
            self.marks.append(b)
        return closed

    def _update_floor(self, row: np.ndarray, ts: float) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            if self.hist_n >= 8:
                # Median je Bin über die letzten Zeilen (np.partition statt des langsamen
                # nanmedian; nie gefüllte Bins bleiben NaN). Der Median ist robust gegen
                # Bursts, folgt aber Dauersignalen – die werten wir unten separat aus.
                h = self.hist[: self.hist_n]
                nan = np.isnan(h)
                k = (self.hist_n - 1) // 2
                f = np.partition(np.where(nan, np.inf, h), k, axis=0)[k]
                f[nan.all(axis=0) | ~np.isfinite(f)] = np.nan
            else:
                f = np.full(len(row), np.nanmedian(row))
            g = float(np.nanmedian(f))
        self.base = g
        self.floor = f.astype(np.float32)
        # schwache, schmale Störlinien des SDR (z.B. HackRF alle 20 MHz): der Boden ragt dort
        # über seine Umgebung hinaus; Bursts genau dort flackern nur um die Schwelle
        if self.grid is not None and np.isfinite(g):
            df = (self.grid[-1] - self.grid[0]) / max(len(self.grid) - 1, 1)
            ff = np.nan_to_num(f, nan=g)
            n = max(3, int(round(2.0 / df)) | 1)
            local = np.convolve(np.pad(ff, n // 2, mode="edge"), np.ones(n) / n, mode="valid")
            spur = ff - local > 4.0
            r = max(1, int(round(0.3 / df)))
            self.spur = np.convolve(spur.astype(np.int8), np.ones(2 * r + 1, dtype=np.int8),
                                    mode="same") > 0
        self._floor_ts = ts
        if self.hist_n >= 8 and np.isfinite(g):
            self._find_steady(ts)

    def _local_base(self, f: np.ndarray, df: float) -> np.ndarray:
        """Grundrauschen der Umgebung (±30 MHz): gleicht die Welligkeit des SDR-Frequenzgangs
        aus, ohne breiten Dauersignalen zu folgen."""
        blk = max(1, int(round(1.0 / df)))                 # ~1-MHz-Blöcke
        nb = len(f) // blk
        if nb < 3:
            return np.full(len(f), self.base)
        coarse = np.median(f[: nb * blk].reshape(nb, blk), axis=1)
        half = max(1, int(round(30.0 / (blk * df))))
        pad = np.pad(coarse, half, mode="edge")
        win = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
        k = int(0.2 * (2 * half))
        low = np.partition(win, k, axis=1)[:, k]
        x = (np.arange(nb) + 0.5) * blk
        return np.interp(np.arange(len(f)), x, low)

    def _find_steady(self, ts: float) -> None:
        """Dauersignale: Bereiche, in denen schon der Median deutlich über dem Grundrauschen liegt."""
        grid, f = self.grid, np.nan_to_num(self.floor, nan=self.base)
        df = (grid[-1] - grid[0]) / max(len(grid) - 1, 1)
        m = f > self._local_base(f, df) + self.threshold
        d = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        if len(starts) > 1:     # Stücke < 2 MHz auseinander gehören zusammen (z.B. Magnetron)
            join = (starts[1:] - ends[:-1]) * df < 2.0
            starts = starts[np.concatenate(([True], ~join))]
            ends = ends[np.concatenate((~join, [True]))]
        out, seen = [], {}
        for a, e in zip(starts, ends):
            b = e - 1
            width = (b - a + 1) * df
            if width < self.MIN_WIDTH and df < 0.3 and b - a < 1:
                continue
            seg = f[a:e]
            wl = 10.0 ** ((seg - seg.max()) / 10.0)
            c = float(np.sum(grid[a:e] * wl) / np.sum(wl)) if width < 4 else float(grid[a] + grid[b]) / 2
            flat = float(np.mean(seg >= seg.max() - 6.0)) if b - a >= 4 else 1.0
            # dasselbe Dauersignal wie bei der letzten Schätzung? (Startzeit, ruhige Frequenz)
            prev = next(((k, v) for k, v in self._steady_since.items()
                         if abs(k - c) <= max(0.6, width / 4)), None)
            if prev is not None:
                c0, t0 = prev[0], prev[1]
                c = 0.8 * c0 + 0.2 * c
            else:
                t0 = ts
            seen[c] = t0
            bu = Burst(c, width, float(grid[a] - df / 2), float(grid[b] + df / 2), float(seg.max()),
                       t0, ts, 0, -1, ts - t0 + 0.0, flat)
            bu.cls, bu.detail = self._classify_steady(bu)
            out.append(bu)
        self._steady_since = seen
        self.steady = out

    def _classify_steady(self, b: Burst) -> tuple[str, str]:
        c, w = b.center, b.width
        if w >= 1.5:
            # stark ausgelasteter AP (> 50 % Sendezeit) steckt ebenfalls im Rauschboden
            span = self._ap_span(c, w)
            if span:
                return "wlan", f"{span[2]} (Dauerlast)"
        if w <= NARROW:
            spur = abs(c - 10 * round(c / 10)) <= 0.25
            return "carrier", f"{c:.0f} MHz" + (" (vermutl. SDR-Störlinie)" if spur else "")
        if self.band == "5" and 6 <= w <= 30:
            ch = fpv_channel(c)
            if ch:
                return "fpv", f"{ch} ({c:.0f} MHz)"
        if b.flat >= 0.6:
            ap = self._ap_containing(b)
            if ap:
                return "wlan", f"{ap} (Dauerlast)"
        elif self.band == "2.4" and 2435 <= c <= 2485 and w >= 8 and b.flat < 0.5:
            # Magnetron: breit, buckelförmig, läuft während des Garens durch
            return "microwave", f"{c:.0f} MHz"
        return "wide", f"Dauersignal {c:.0f} MHz"

    def _ap_containing(self, b: Burst) -> str | None:
        """SSID des bekannten APs, in dessen Kanal der (Teil-)Burst vollständig liegt."""
        lo, hi = b.center - b.width / 2, b.center + b.width / 2
        best = None
        for ac, aw, ssid in self.aps:
            tol = max(1.5, 0.05 * aw)
            if ac - aw / 2 - tol <= lo and hi <= ac + aw / 2 + tol:
                key = (aw, abs(ac - b.center))
                if best is None or key < best[0]:
                    best = (key, ssid)
        return None if best is None else best[1]

    def _segments(self, row: np.ndarray) -> list[tuple]:
        grid = self.grid
        df = (grid[-1] - grid[0]) / max(len(grid) - 1, 1)
        thr = np.nan_to_num(self.floor, nan=np.inf) + self.threshold
        rs = np.nan_to_num(row, nan=-300.0)
        # geglättet (~0,4 MHz, in dB) gegen die starke Streuung einzelner FFT-Bins – auch
        # OFDM-Signale haben ohne Mittelung tiefe Einbrüche einzelner Bins. Rand mit dem
        # Randwert auffüllen, nicht mit 0 dB.
        k = max(1, int(round(0.4 / df))) | 1
        sm = np.convolve(np.pad(rs, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
        # Hysterese: Segmente reichen bis 4 dB unter die Schwelle, zählen aber nur,
        # wenn sie die Schwelle irgendwo überschreiten (stabile Breite bei schwachen Signalen)
        with np.errstate(invalid="ignore"):
            fin = np.isfinite(row)
            m_hi = fin & (sm > thr)
            m = fin & (sm > thr - 4.0)
        d = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)
        # Lücken bis ~0,4 MHz schließen (zerfranste Flanken eines Signals)
        gap = max(1, int(round(0.4 / df)))
        if len(starts) > 1:
            join = (starts[1:] - ends[:-1]) <= gap
            starts = starts[np.concatenate(([True], ~join))]
            ends = ends[np.concatenate((~join, [True]))]
        if len(starts):
            c = np.concatenate(([0], np.cumsum(m_hi)))
            ok = (c[ends] - c[starts]) > 0
            starts, ends = starts[ok], ends[ok]
        if len(starts) > self.MAX_SEGMENTS:     # Übersteuerung o.ä.: die stärksten behalten
            pk = np.array([np.nanmax(row[s:e]) for s, e in zip(starts, ends)])
            keep = np.sort(np.argsort(pk)[-self.MAX_SEGMENTS:])
            starts, ends = starts[keep], ends[keep]
        out = []
        stack = list(zip(starts.tolist(), ends.tolist()))
        while stack and len(out) < self.MAX_SEGMENTS:
            s, e = stack.pop()
            if e <= s or not np.any(sm[s:e] > thr[s:e]):
                continue
            seg = sm[s:e]
            ipk = int(np.argmax(seg))
            pk = float(np.max(rs[s:e]))
            lvl = max(float(seg[ipk]) - 10.0, float(np.min(thr[s:e])) - 4.0)
            # Kern: zusammenhängend um die Spitze, bis 10 dB darunter
            below = np.flatnonzero(seg < lvl)
            left, right = below[below < ipk], below[below > ipk]
            a = s + (int(left[-1]) + 1 if len(left) else 0)
            b = s + (int(right[0]) - 1 if len(right) else e - s - 1)
            # Flanken: so weit es abwärts geht; dahinter liegende Spitzen sind eigene Bursts
            lo_i, hi_i = a, b
            while lo_i > s and sm[lo_i - 1] <= sm[lo_i] + 1.0:
                lo_i -= 1
            while hi_i < e - 1 and sm[hi_i + 1] <= sm[hi_i] + 1.0:
                hi_i += 1
            stack.append((s, lo_i))
            stack.append((hi_i + 1, e))
            width = (b - a + 1) * df
            if df < 0.3 and width < self.MIN_WIDTH:
                continue                        # Rauschspitze einzelner FFT-Bins
            if width < 1.0 and self.spur is not None and self.spur[s + ipk] \
                    and pk < float(self.floor[s + ipk]) + self.threshold + 6.0:
                continue                        # flackernde Störlinie
            if width < 4:   # Leistungsschwerpunkt (flache Kuppen: argmax läge am Rand)
                wl = 10.0 ** ((rs[a:b + 1] - pk) / 10.0)
                center = float(np.sum(grid[a:b + 1] * wl) / np.sum(wl))
            else:
                center = float(grid[a] + grid[b]) / 2
            # Flachheit: Anteil des Kerns höchstens 6 dB unter der (geglätteten) Spitze.
            # OFDM (WLAN) ist flach (≈ 0,8–1), Buckel wie Mikrowellen deutlich weniger.
            flat = 1.0
            if b - a >= 4:
                k = sm[a + 1:b]
                flat = float(np.mean(k >= np.max(k) - 6.0))
            out.append([float(grid[lo_i] - df / 2), float(grid[hi_i] + df / 2),
                        center, float(width), pk, flat, None])
        out.sort(key=lambda x: x[2])
        self._row_context(out)
        return out

    def _row_context(self, segs: list) -> None:
        """Schmale Stücke neben einem flachen WLAN-Stück im selben Kanal gehören zum Frame.

        Ein Sweep-SDR erfasst einen kurzen WLAN-Frame oft nur in Teilen; die Ränder dieser
        Teile sehen aus wie schmale Bursts (Bluetooth, Zigbee …)."""
        wide = [x for x in segs if x[3] > NARROW and x[5] >= 0.6]
        if not wide:
            return
        spans = []
        for w in wide:
            span = self._ap_span(w[2], w[3])
            if span:
                spans.append(span)
            elif not self.aps:      # ohne Scan: Umgebung des Stücks
                spans.append((w[2] - 10.0, w[2] + 10.0, ""))
        if not spans:
            return
        for x in segs:
            if x[3] > NARROW:
                continue
            for span in spans:
                lo, hi = span[:2]
                if lo - 0.5 <= x[2] <= hi + 0.5:
                    x[6] = span[2]
                    break

    def _ap_span(self, center: float, width: float):
        """(lo, hi, SSID) des bekannten APs, dessen Kanal das Stück enthält."""
        best = None
        for ac, aw, ssid in self.aps:
            lo, hi = ac - aw / 2, ac + aw / 2
            tol = max(1.5, 0.05 * aw)
            if lo - tol <= center - width / 2 and center + width / 2 <= hi + tol:
                key = (aw, abs(ac - center))
                if best is None or key < best[0]:
                    best = (key, (lo, hi, ssid))
        return None if best is None else best[1]

    def _update_wlan_spans(self) -> None:
        """Kanäle mit WLAN-Aktivität im Zeitfenster (dort sind schmale Bursts mehrdeutig)."""
        # bekannte APs senden mindestens alle ~100 ms Beacons: ihre Kanäle gelten als aktiv
        spans = [(ac - aw / 2, ac + aw / 2) for ac, aw, _ in self.aps]
        wl = [b for b in self.recent if b.cls == "wlan"]
        for b in wl:   # Aktivität ohne bekannten AP (z.B. kein Scan)
            if b.width > NARROW and not any(lo <= b.center <= hi for lo, hi in spans):
                spans.append((b.center - 10.0, b.center + 10.0))
        self.wlan_spans = spans

    def _in_wlan(self, c: float, margin: float = 0.5) -> bool:
        return any(lo - margin <= c <= hi + margin for lo, hi in self.wlan_spans)

    def _track(self, segs, ts: float, idx: int) -> list[Burst]:
        used = set()
        for lo, hi, c, w, pk, flat, ctx in segs:
            best = None
            for i, tr in enumerate(self.tracks):
                if i in used or lo > tr.hi or hi < tr.lo:
                    continue
                if abs(c - tr.center) > max(0.6, 0.25 * max(w, tr.width)):
                    continue
                d = abs(c - tr.center)
                if best is None or d < best[0]:
                    best = (d, i)
            if best is None:
                self.tracks.append(_Track(lo, hi, c, w, pk, ts, ts, idx, idx, 1, [c], flat, ctx))
                used.add(len(self.tracks) - 1)
            else:
                tr = self.tracks[best[1]]
                used.add(best[1])
                tr.lo, tr.hi = min(tr.lo, lo), max(tr.hi, hi)
                if w >= tr.width:
                    tr.width, tr.flat = w, flat
                tr.peak = max(tr.peak, pk)
                if ctx is not None and tr.wlan_ctx is None:
                    tr.wlan_ctx = ctx
                tr.t1, tr.row_last = ts, idx
                tr.rows += 1
                if len(tr.centers) < 64:
                    tr.centers.append(c)
                tr.center = float(np.median(tr.centers))
        closed, keep = [], []
        for i, tr in enumerate(self.tracks):
            # länger bestehende Signale dürfen eine Zeile aussetzen (Schwankung um die Schwelle)
            if i in used or (tr.rows >= 3 and idx - tr.row_last <= (3 if tr.rows >= 10 else 1)):
                keep.append(tr)
            else:
                closed.append(self._to_burst(tr))
        self.tracks = keep
        return closed

    def _to_burst(self, tr: _Track) -> Burst:
        return Burst(tr.center, tr.width, tr.lo, tr.hi, tr.peak, tr.t0, tr.t1, tr.rows,
                     tr.row_last, (tr.t1 - tr.t0) + self.row_dt, tr.flat, tr.wlan_ctx)

    def _expire(self, now: float) -> None:
        while self.recent and self.recent[0].t1 < now - self.window:
            b = self.recent.popleft()
            if b.counted:
                f = int(round(b.center))
                self.narrow[f] -= 1
                if self.narrow[f] <= 0:
                    del self.narrow[f]

    # -- Klassifikation -------------------------------------------------------

    def _classify(self, b: Burst) -> None:
        cls, detail = self.classify(b)
        b.cls, b.detail = cls, detail

    def classify(self, b: Burst) -> tuple[str, str]:
        # Dauersignale (Träger, FPV …) werden über den Rauschboden erkannt (_find_steady)
        band, c, w = self.band, b.center, b.width
        if b.wlan_ctx is not None:
            return "wlan", b.wlan_ctx or "Teilstück"
        # Spur endet, weil das Signal andauert und im Rauschboden (Median) aufgeht
        for st in self.steady:
            if abs(st.center - c) <= max(1.0, w / 4) and st.lo <= c <= st.hi:
                return st.cls, st.detail

        # WLAN: flaches OFDM-Spektrum. Sweep-SDRs erfassen einen kurzen Frame oft nur
        # teilweise (hackrf_sweep: 5-MHz-Streifen) – daher auch Teilstücke zuordnen.
        if w > NARROW and b.flat >= 0.6:
            ap = self._ap_containing(b)
            if ap:
                return "wlan", ap
            # analoges FPV-Video sendet ununterbrochen – WLAN nie (auch wenn die Frequenz
            # auf ein WLAN-Raster fällt, z.B. R1 5658 ≈ Kanal 132)
            if band == "5":
                fpv = self._fpv(b)
                if fpv:
                    return "fpv", fpv
                if 5850 <= c <= 5930 and 6 <= w <= 13 and its_channel(c) is not None:
                    return "its_g5", str(its_channel(c))
            ww = wlan_match(band, c, w) if w >= 12 else None
            if ww:
                return "wlan", f"{ww} MHz"
            if b.flat >= 0.7:
                return "wlan", "Teilstück"

        if band == "2.4":
            return self._classify_24(b)
        if band == "5":
            return self._classify_5(b)
        return ("narrow", "") if w <= NARROW else ("wide", "")

    def _classify_24(self, b: Burst) -> tuple[str, str]:
        c, w = b.center, b.width
        if w <= NARROW:
            counts = self.narrow.copy()
            counts[int(round(c))] += 1          # aktuellen Burst mitzählen
            # Kanäle außerhalb aktiver WLAN-Kanäle: dort sind schmale Bursts eindeutig
            free = [f for f in range(BT_LO, BT_HI + 1) if not self._in_wlan(f)]
            # typische Belegung eines Kanals durch zufälliges Springen (Median ist robust
            # gegen die gehäuften Kanäle selbst)
            typ = float(np.median([counts[f] for f in free])) if free else 0.0
            busy = max(3.0, 2.5 * typ + 3.0)
            adv = next((ch for f, ch in ADV_BY_FREQ.items() if abs(c - f) <= 1.0), None)
            if adv is not None:
                n = counts[int(BLE_ADV[adv])]
                adv_hits = sum(1 for f in ADV_BY_FREQ if counts[int(f)] >= 2)
                if n >= busy or adv_hits >= 2:
                    return "ble_adv", str(adv)
            if self._in_wlan(c):
                # In einem aktiven WLAN-Kanal sind schmale Bursts meist Bruchstücke von
                # WLAN-Frames, die der Sweep nur teilweise erfasst hat
                span = self._ap_span(c, w)
                return "wlan", (span[2] if span else "Teilstück")
            # 802.15.4 (O-QPSK, 2 Mchip/s) belegt ~2 MHz und liegt genau im 5-MHz-Raster
            zb = next((ch for ch, f in ZIGBEE.items() if abs(c - f) <= 0.4), None)
            if zb is not None and w >= 1.2 and counts[int(ZIGBEE[zb])] >= busy:
                return "zigbee", f"Z{zb}"
            fi = int(round(c))
            if counts[fi] >= busy:
                # immer wieder dieselbe, einzeln stehende Frequenz: kein Frequenzsprung.
                # Sind auch die Nachbarn dicht belegt, ist es eher ein breiter Bereich
                # (z.B. Rand eines nicht gescannten WLANs).
                if counts[fi - 1] < busy or counts[fi + 1] < busy:
                    if counts[fi - 2] < busy and counts[fi + 2] < busy:
                        return "narrow", f"wiederholt {fi} MHz"
                return "narrow", ""
            # Frequenzsprung: viele verschiedene, jeweils selten belegte Frequenzen außerhalb
            # der WLAN-Kanäle (Advertising- und fest belegte Kanäle zählen nicht)
            hop = sum(1 for f in free if 0 < counts[f] < busy
                      and all(abs(f - a) > 1 for a in ADV_BY_FREQ))
            need = max(4, min(8, len(free) // 6))
            if hop >= need and BT_LO - 1 <= c <= BT_HI + 1:
                return "bt", ""
            return "narrow", ""
        # breit, nicht auf WLAN-Raster
        if 2425 <= c <= 2495:
            n = 1 + sum(1 for x in self.recent if x.width > NARROW and 2425 <= x.center <= 2495
                        and x.cls in ("microwave", "wide"))
            if n >= 3:
                return "microwave", ""
        return "wide", ""

    def _classify_5(self, b: Burst) -> tuple[str, str]:
        c, w = b.center, b.width
        if 5850 <= c <= 5930 and 6 <= w <= 13:
            ch = its_channel(c)
            if ch is not None:
                return "its_g5", str(ch)
        fpv = self._fpv(b)
        if fpv:
            return "fpv", fpv
        if w <= NARROW:
            # Radar: kurze, schmale Pulse immer wieder auf derselben Frequenz – mit Abstand zu
            # aktiven WLAN-Kanälen (deren Bruchstücke an den Kanalrändern sähen ähnlich aus)
            if 5250 <= c <= 5725 and b.rows <= 2 and not self._in_wlan(c, 10.0):
                n = 1 + sum(1 for x in self.recent if x.width <= NARROW and abs(x.center - c) <= 1.0
                            and x.rows <= 2)
                if n >= 3:
                    return "radar", "Wetterradar?" if 5600 <= c <= 5650 else ""
            return "narrow", ""
        return "wide", ""

    FPV_MIN_DURATION = 1.5   # s

    def _fpv(self, b: Burst) -> str:
        """FPV-Kanal, wenn der Burst andauert oder Teil eines bereits erkannten Senders ist."""
        if not 6 <= b.width <= 30:
            return ""
        ch = fpv_channel(b.center)
        if ch is None:
            return ""
        c = b.center
        if b.duration >= self.FPV_MIN_DURATION and b.rows >= 5 \
                or any(x.cls == "fpv" and abs(x.center - c) <= 6 for x in self.recent) \
                or any(t.t1 - t.t0 >= self.FPV_MIN_DURATION and abs(t.center - c) <= 6
                       for t in self.tracks):
            return f"{ch} ({c:.0f} MHz)"
        return ""

    # -- Auswertung ----------------------------------------------------------
    def active_tracks(self, now: float | None = None) -> list[Burst]:
        """Gerade andauernde Signale (≥ 1 s), vorläufig klassifiziert."""
        out = []
        stale = 2.0 + 3 * self.row_dt
        for tr in self.tracks:
            if tr.t1 - tr.t0 >= 1.0 and (now is None or tr.t1 >= now - stale):
                b = self._to_burst(tr)
                self._classify(b)
                out.append(b)
        return out

    def findings(self, now: float) -> list[Finding]:
        self._expire(now)
        by: dict[str, list[Burst]] = collections.defaultdict(list)
        for b in self.recent:
            by[b.cls].append(b)
        active = self.active_tracks(now)
        if now - self._floor_ts <= 3.0:
            active += self.steady
        for b in active:
            by[b.cls].append(b)
        span = self.window
        if self.recent:
            span = min(self.window, max(1.0, now - self.recent[0].t0))
        out = []
        for key in ORDER:
            bs = by.get(key)
            if not bs:
                continue
            sc = CLASSES[key]
            out.append(Finding(key, sc.name, sc.color, sc.hint, len(bs), len(bs) / span,
                               self._where(key, bs), max(b.peak for b in bs),
                               max(b.t1 for b in bs), any(b in active for b in bs)))
        return out

    def _where(self, key: str, bs: list[Burst]) -> str:
        if key == "bt":
            n = len({int(round(b.center)) for b in bs})
            return f"{n} Frequenzen {min(b.center for b in bs):.0f}–{max(b.center for b in bs):.0f} MHz"
        if key == "carrier":
            fs = sorted({round(b.center) for b in bs})
            more = f" +{len(fs) - 5}" if len(fs) > 5 else ""
            return (f"{len(fs)} Linien: " if len(fs) > 1 else "") + \
                ", ".join(f"{f}" for f in fs[:5]) + more + " MHz"
        if key in ("ble_adv", "zigbee", "its_g5", "fpv", "wlan", "radar"):
            cnt = collections.Counter(b.detail or f"{b.center:.0f} MHz" for b in bs)
            if key == "wlan":   # Kanal statt Breite anzeigen, wenn kein AP bekannt
                cnt = collections.Counter(
                    b.detail if not b.detail.endswith("MHz") else f"{b.center:.0f} MHz ({b.detail})"
                    for b in bs)
            items = [k for k, _ in cnt.most_common(4)]
            prefix = "Kanal " if key in ("ble_adv", "its_g5") else ""
            more = f" +{len(cnt) - 4}" if len(cnt) > 4 else ""
            return prefix + ", ".join(sorted(items) if key != "wlan" else items) + more
        rep = sorted({b.detail for b in bs if b.detail.startswith("wiederholt")})
        if rep:
            fs = ", ".join(d.split()[1] for d in rep[:4])
            return f"wiederholt auf {fs} MHz" + (f" +{len(rep) - 4}" if len(rep) > 4 else "")
        lo = min(b.center for b in bs)
        hi = max(b.center for b in bs)
        return f"{lo:.0f} MHz" if hi - lo < 1 else f"{lo:.0f}–{hi:.0f} MHz"

    def marker_at(self, freq: float, row_abs: float, df: float, drow: float = 1.5) -> Burst | None:
        """Markierung nahe (Frequenz, absolute Zeile) für die Mauszeiger-Info."""
        best = None
        for b in self.marks:
            if b.row_end - b.rows + 1 - drow <= row_abs <= b.row_end + drow \
                    and b.lo - df <= freq <= b.hi + df:
                d = abs(freq - b.center)
                if best is None or d < best[0]:
                    best = (d, b)
        return None if best is None else best[1]


def format_duration(s: float) -> str:
    if not math.isfinite(s):
        return "?"
    return f"{s * 1000:.0f} ms" if s < 1 else f"{s:.1f} s"
