"""Gemeinsame Infrastruktur für SDR-Sweep-Backends."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

try:      # scipy rechnet complex64 in einfacher Genauigkeit – ca. 4× schneller als numpy (immer double)
    from scipy import fft as _fft
except ImportError:  # pragma: no cover
    _fft = None


@dataclass
class Sweep:
    freqs_mhz: np.ndarray     # Mittenfrequenz jedes Bins
    power_db: np.ndarray      # Leistung (dBFS bzw. hackrf-dB)
    f_lo: float               # angeforderter Bereich (MHz)
    f_hi: float
    timestamp: float = field(default_factory=time.time)
    duration: float = 0.0     # Dauer des Sweeps in s


@dataclass
class Param:
    key: str
    label: str
    kind: str                 # "int", "float", "bool", "choice", "str"
    default: object
    minimum: float = 0
    maximum: float = 0
    step: float = 1
    choices: tuple = ()
    live: bool = False        # ohne Neustart änderbar
    suffix: str = ""


class SdrBackend:
    """Ein Backend erzeugt in einem eigenen Thread fortlaufend Sweeps."""

    name = "basis"
    f_min_mhz = 1.0
    f_max_mhz = 6000.0
    params: list[Param] = []

    def __init__(self, settings: dict | None = None):
        self.settings = {p.key: p.default for p in self.params}
        if settings:
            self.settings.update({k: v for k, v in settings.items() if k in self.settings})
        self.sweeps: queue.Queue[Sweep] = queue.Queue(maxsize=64)
        self.errors: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.range = (2400.0, 2500.0)
        self._lock = threading.Lock()
        self._pending: dict = {}
        self.state = ""           # Statustext während der Initialisierung

    # -- Steuerung --------------------------------------------------------
    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, ""

    def start(self, f_lo: float, f_hi: float) -> None:
        self.stop()
        lo = max(f_lo, self.f_min_mhz)
        hi = min(f_hi, self.f_max_mhz)
        if hi <= lo:
            self.errors.put(f"{self.name}: Bereich {f_lo:.0f}–{f_hi:.0f} MHz nicht abgedeckt "
                            f"(Gerät: {self.f_min_mhz:.0f}–{self.f_max_mhz:.0f} MHz)")
            return
        self.range = (lo, hi)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_safe, name=f"sdr-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._shutdown_hook()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def update_setting(self, key: str, value) -> bool:
        """Gibt True zurück, wenn die Änderung live übernommen wird."""
        self.settings[key] = value
        p = next((p for p in self.params if p.key == key), None)
        if p is not None and p.live:
            with self._lock:
                self._pending[key] = value
            return True
        return False

    def take_pending(self) -> dict:
        with self._lock:
            d, self._pending = self._pending, {}
        return d

    # -- intern -----------------------------------------------------------
    def _emit(self, sweep: Sweep) -> None:
        try:
            self.sweeps.put_nowait(sweep)
        except queue.Full:
            try:
                self.sweeps.get_nowait()
            except queue.Empty:
                pass
            self.sweeps.put_nowait(sweep)

    def _run_safe(self) -> None:
        try:
            self.run()
        except Exception as exc:  # noqa: BLE001 - Fehler an die GUI melden
            self.errors.put(f"{self.name}: {exc}")

    def _shutdown_hook(self) -> None:
        pass

    def close(self) -> None:
        """Backend endgültig beenden (Geräte/Prozesse freigeben)."""
        self.stop()

    def run(self) -> None:
        raise NotImplementedError

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()


def psd_dbfs(samples: np.ndarray, fft_size: int, window: np.ndarray) -> np.ndarray:
    """Gemittelte Leistungsdichte (Welch, ohne Überlappung) in dBFS, fftshifted."""
    n = (len(samples) // fft_size) * fft_size
    if n == 0:
        return np.full(fft_size, -150.0)
    segs = samples[:n].reshape(-1, fft_size) * window
    spec = _fft.fft(segs, axis=1, overwrite_x=True) if _fft is not None else np.fft.fft(segs, axis=1)
    p = np.mean(spec.real ** 2 + spec.imag ** 2, axis=0) / (np.sum(window, dtype=np.float64) ** 2)
    return 10.0 * np.log10(np.fft.fftshift(p) + 1e-20)


def resample_to_grid(freqs: np.ndarray, power: np.ndarray, f0: float, f1: float,
                     nbins: int) -> np.ndarray:
    """Spektrum auf ein festes Raster abbilden (Max pro Rasterbin, Lücken interpoliert)."""
    out = np.full(nbins, -np.inf)
    if len(freqs) == 0:
        return np.full(nbins, np.nan)
    df = (f1 - f0) / nbins
    idx = np.floor((freqs - f0) / df).astype(np.int64)
    ok = (idx >= 0) & (idx < nbins) & np.isfinite(power)
    np.maximum.at(out, idx[ok], power[ok])
    filled = np.isfinite(out)
    if not filled.any():
        return np.full(nbins, np.nan)
    centers = f0 + (np.arange(nbins) + 0.5) * df
    if not filled.all():
        fmin, fmax = freqs[ok].min(), freqs[ok].max()
        out[~filled] = np.interp(centers[~filled], centers[filled], out[filled])
        out[(centers < fmin - df) | (centers > fmax + df)] = np.nan
    return out


class GridMapper:
    """Schnelles, wiederverwendbares Abbilden von Sweeps auf ein festes Raster.

    Die Frequenzachse eines Backends ändert sich zwischen Sweeps praktisch nie. Die Zuordnung
    Bin -> Rasterpunkt (inkl. Interpolation leerer Rasterpunkte) wird deshalb einmal berechnet
    und danach per ``np.maximum.reduceat`` angewendet. Mehrere Sweeps mit derselben Achse
    werden vorher per Maximum zusammengefasst (nur ein Abbilden pro Wasserfallzeile).
    """

    def __init__(self):
        self._key = None
        self._plan = None

    def _make_plan(self, freqs: np.ndarray, f0: float, f1: float, nbins: int):
        df = (f1 - f0) / nbins
        idx = np.floor((freqs - f0) / df).astype(np.int64)
        ok = np.flatnonzero((idx >= 0) & (idx < nbins))
        if len(ok) == 0 or np.any(np.diff(idx[ok]) < 0):
            return None                                   # unsortiert: allgemeiner Weg
        sel = idx[ok]
        starts = np.flatnonzero(np.r_[True, sel[1:] != sel[:-1]])
        targets = sel[starts]
        filled = np.zeros(nbins, bool)
        filled[targets] = True
        centers = f0 + (np.arange(nbins) + 0.5) * df
        fmin, fmax = freqs[ok].min(), freqs[ok].max()
        outside = (centers < fmin - df) | (centers > fmax + df)
        gaps = np.flatnonzero(~filled & ~outside)
        return ok, starts, targets, gaps, centers, np.flatnonzero(outside)

    def map(self, sweeps: list, f0: float, f1: float, nbins: int) -> np.ndarray | None:
        """sweeps: Liste von (freqs, power). Ergebnis: Maximum aller Sweeps auf dem Raster."""
        row = None
        groups: dict = {}
        for freqs, power in sweeps:
            if len(freqs) == 0:
                continue
            key = (len(freqs), float(freqs[0]), float(freqs[-1]))
            g = groups.get(key)
            if g is None:
                groups[key] = [freqs, power]
            else:
                g[1] = np.fmax(g[1], power)
        for (n, a, b), (freqs, power) in groups.items():
            r = self._map_one(freqs, power, f0, f1, nbins, (n, a, b, f0, f1, nbins))
            row = r if row is None else np.fmax(row, r)
        return row

    def _map_one(self, freqs, power, f0, f1, nbins, key) -> np.ndarray:
        if key != self._key:
            self._key = key
            self._plan = self._make_plan(freqs, f0, f1, nbins)
        plan = self._plan
        if plan is None or not np.isfinite(power).all():
            return resample_to_grid(freqs, power, f0, f1, nbins)
        ok, starts, targets, gaps, centers, outside = plan
        out = np.empty(nbins, dtype=np.float64)
        out[targets] = np.maximum.reduceat(power[ok], starts)
        if len(gaps):
            out[gaps] = np.interp(centers[gaps], centers[targets], out[targets])
        out[outside] = np.nan
        return out
