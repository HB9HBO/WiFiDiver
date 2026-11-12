"""HackRF One über `hackrf_sweep` (bis ~8 GHz/s Sweep-Rate)."""

from __future__ import annotations

import collections
import io
import os
import math
import shutil
import subprocess
import threading
import time

import numpy as np

from .base import Param, SdrBackend, Sweep


class SweepParser:
    """Zerlegt die Binärausgabe von ``hackrf_sweep -B`` in vollständige Sweeps.

    Datensatz: uint32 Länge (ohne dieses Feld), uint64 hz_low, uint64 hz_high, float32 dB × n.
    Die Datensätze eines Sweeps kommen verschränkt (2400, 2410, 2405, 2415, … MHz); ein neuer
    Sweep beginnt, sobald wieder die niedrigste Startfrequenz erscheint. Ein Sweep wird
    ausgegeben, wenn der nächste beginnt.
    """

    def __init__(self):
        self.buf = bytearray()
        self.dtype: np.dtype | None = None
        self.rec_len = 0
        self.pending: list[np.ndarray] = []
        self.start_lo: int | None = None
        self._freq_key = b""
        self._freqs = np.zeros(0)

    def feed(self, chunk: bytes) -> list[tuple[np.ndarray, np.ndarray]]:
        self.buf += chunk
        if self.dtype is None:
            if len(self.buf) < 4:
                return []
            self.rec_len = int.from_bytes(self.buf[:4], "little")
            nb = (self.rec_len - 16) // 4
            if nb <= 0 or self.rec_len != 16 + 4 * nb:
                raise ValueError(f"unerwartetes Binärformat (Länge {self.rec_len})")
            self.dtype = np.dtype([("len", "<u4"), ("lo", "<u8"), ("hi", "<u8"), ("p", "<f4", (nb,))])
        cnt = len(self.buf) // self.dtype.itemsize
        if cnt == 0:
            return []
        size = cnt * self.dtype.itemsize
        recs = np.frombuffer(bytes(self.buf[:size]), self.dtype)
        del self.buf[:size]
        if np.any(recs["len"] != self.rec_len):
            raise ValueError("Binärdaten von hackrf_sweep nicht synchron")
        lo = recs["lo"]
        m = int(lo.min())
        self.start_lo = m if self.start_lo is None else min(self.start_lo, m)
        cuts = np.flatnonzero(lo == self.start_lo)
        if len(cuts) and cuts[0] == 0 and not self.pending:
            cuts = cuts[1:]
        out = []
        a = 0
        for c in cuts:
            self.pending.append(recs[a:c])
            a = c
            sw = self._assemble(self.pending)
            if sw is not None:
                out.append(sw)
            self.pending = []
        self.pending.append(recs[a:])
        return out

    def _assemble(self, parts: list[np.ndarray]):
        recs = np.concatenate(parts) if len(parts) > 1 else parts[0]
        if len(recs) == 0:
            return None
        lo = recs["lo"]
        order = np.argsort(lo, kind="stable")
        key = lo[order].tobytes() + recs["hi"][order].tobytes()
        if key != self._freq_key:
            # Frequenzachse nur neu berechnen, wenn sich die Datensätze ändern (sonst wiederverwenden)
            nb = recs["p"].shape[1]
            lo_f = lo[order].astype(np.float64)
            bw = (recs["hi"][order].astype(np.float64) - lo_f) / nb
            self._freqs = ((lo_f[:, None] + (np.arange(nb) + 0.5) * bw[:, None]) / 1e6).ravel()
            self._freqs.flags.writeable = False
            self._freq_key = key
        return self._freqs, recs["p"][order].ravel()


class HackRFBackend(SdrBackend):
    name = "HackRF"
    f_min_mhz = 1.0
    f_max_mhz = 7250.0
    params = [
        Param("serial", "Seriennummer", "str", ""),
        Param("bin_width", "Bin-Breite", "int", 100_000, 2445, 5_000_000, 1000, suffix=" Hz"),
        Param("lna", "LNA-Gain (IF)", "int", 32, 0, 40, 8, suffix=" dB"),
        Param("vga", "VGA-Gain (BB)", "int", 20, 0, 62, 2, suffix=" dB"),
        Param("amp", "RF-Verstärker", "bool", False),
        Param("antenna_power", "Antennen-Speisung", "bool", False),
    ]

    def __init__(self, settings: dict | None = None):
        super().__init__(settings)
        self.proc: subprocess.Popen | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if not shutil.which("hackrf_sweep"):
            return False, "hackrf_sweep nicht gefunden (Paket hackrf installieren)"
        return True, ""

    def _cmd(self) -> list[str]:
        s = self.settings
        lo = int(math.floor(self.range[0]))
        hi = int(math.ceil(self.range[1]))
        if hi - lo < 20:  # hackrf_sweep schrittweite 20 MHz
            hi = lo + 20
        cmd = ["hackrf_sweep", "-n", "-f", f"{lo}:{hi}",
               "-w", str(int(s["bin_width"])),
               "-l", str(int(s["lna"]) // 8 * 8),
               "-g", str(int(s["vga"]) // 2 * 2),
               "-a", "1" if s["amp"] else "0",
               "-p", "1" if s["antenna_power"] else "0"]
        if s["serial"]:
            cmd += ["-d", str(s["serial"])]
        return cmd

    def _read_stderr(self, stream) -> None:
        for line in stream:
            line = line.strip()
            if line:
                self.stderr_tail.append(line)

    def _shutdown_hook(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()

    STALL_TIMEOUT = 3.0      # s ohne Daten -> hackrf_sweep neu starten
    RESTART_DELAY = 0.5

    def _watchdog(self, proc: subprocess.Popen) -> None:
        """Beendet hackrf_sweep, wenn es läuft, aber keine Daten mehr liefert."""
        while proc.poll() is None and not self.stopped:
            time.sleep(0.25)
            if time.time() - self._last_data > self.STALL_TIMEOUT:
                self._stalled = True
                proc.terminate()
                return

    def run(self) -> None:
        """Startet hackrf_sweep und startet es neu, wenn es abbricht.

        hackrf_sweep beendet sich bei USB-Übertragungsfehlern ("streaming stopped (-1003)")
        mit Exit-Code 0, z.B. wenn ein anderes Gerät am selben USB-Hub (WLAN-Stick beim
        Scannen) stört. Manchmal läuft es auch weiter, ohne Daten zu liefern.
        """
        self.restarts = 0
        fails = 0
        while not self.stopped:
            got = self._run_once()
            if self.stopped:
                return
            err = next((l for l in reversed(self.stderr_tail) if "-100" in l or "rror" in l), "")
            if self._stalled:
                reason = "keine Daten"
            elif "streaming stopped" in err:
                reason = "USB-Übertragung abgebrochen"
            else:
                reason = err or f"Exit-Code {self.proc.returncode}"
            fails = 0 if got else fails + 1
            if fails >= 5:
                raise RuntimeError(f"hackrf_sweep liefert keine Daten: {reason}")
            self.restarts += 1
            self.state = f"hackrf_sweep neu gestartet ({self.restarts}×, zuletzt: {reason})"
            self._stop.wait(self.RESTART_DELAY * (1 + fails))

    def _run_once(self) -> int:
        """Ein hackrf_sweep-Lauf; gibt die Anzahl gelieferter Sweeps zurück.

        Liest die Binärausgabe (-B): je Datensatz uint32 Länge, uint64 hz_low, uint64 hz_high,
        dann float32-Leistungswerte. Das spart das Zerlegen von Textzeilen in Python, das bei
        2.4 GHz (~80 Sweeps/s) sonst den größten Teil der CPU-Zeit kostet.
        """
        self.stderr_tail.clear()
        self._stalled = False
        self._last_data = time.time() + 2.0      # Gerät braucht etwas zum Starten
        self.proc = proc = subprocess.Popen(self._cmd() + ["-B"], stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0)
        err = io.TextIOWrapper(proc.stderr, errors="replace")
        threading.Thread(target=self._read_stderr, args=(err,), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(proc,), daemon=True).start()

        fd = proc.stdout.fileno()
        parser = SweepParser()
        n = 0
        t_start = time.time()
        while not self.stopped:
            try:
                chunk = os.read(fd, 1 << 18)
            except OSError:
                break
            if not chunk:
                break
            self._last_data = time.time()
            try:
                sweeps = parser.feed(chunk)
            except ValueError as exc:
                self.stderr_tail.append(str(exc))
                break
            for freqs, power in sweeps:
                now = time.time()
                self._emit(Sweep(freqs, power, self.range[0], self.range[1], now, now - t_start))
                t_start = now
                n += 1
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        time.sleep(0.05)   # stderr-Thread die letzten Zeilen lesen lassen
        return n
