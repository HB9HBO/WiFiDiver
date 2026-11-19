"""AD9361-Gerät mit Pluto-Firmware (HamGeek E310, ANTSDR) über das Netz.

Auf dem Gerät läuft ``e310d`` (helper/e310): Es sweept, rechnet die FFT und schickt pro Sweep
nur die Leistungswerte (int16 in 0.01 dB). Hier wird nur noch empfangen und umgerechnet –
praktisch ohne CPU-Last, auch auf einem Raspberry Pi.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import numpy as np

from .base import Param, SdrBackend, Sweep

MAGIC = b"E31D"
T_INFO, T_SWEEP, T_ERROR, T_STATE = 1, 2, 3, 4
_HDR = struct.Struct("<4sBBHI")
_SWEEP_HDR = struct.Struct("<IIddff")
NO_DATA = -32768
MAX_FRAME = 4 << 20


class FrameParser:
    """Zerlegt den Datenstrom von e310d in Rahmen (typ, nutzdaten)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[int, bytes]]:
        self.buf += chunk
        out = []
        while len(self.buf) >= _HDR.size:
            magic, typ, _ver, _res, n = _HDR.unpack_from(self.buf)
            if magic != MAGIC or n > MAX_FRAME:
                raise ValueError("Datenstrom von e310d nicht synchron")
            if len(self.buf) < _HDR.size + n:
                break
            out.append((typ, bytes(self.buf[_HDR.size:_HDR.size + n])))
            del self.buf[:_HDR.size + n]
        return out


class SweepDecoder:
    """SWEEP-Nutzdaten -> (Frequenzen in MHz, Leistung in dB); die Frequenzachse wird zwischengespeichert."""

    def __init__(self):
        self._key = None
        self._freqs = np.zeros(0)

    def decode(self, payload: bytes) -> tuple[np.ndarray, np.ndarray, float, float]:
        seq, nb, f0, df, dur, gain = _SWEEP_HDR.unpack_from(payload)
        raw = np.frombuffer(payload, "<i2", count=nb, offset=_SWEEP_HDR.size)
        key = (nb, f0, df)
        if key != self._key:
            self._freqs = (f0 + df * np.arange(nb)) / 1e6
            self._key = key
        p = raw.astype(np.float32) * np.float32(0.01)
        p[raw == NO_DATA] = np.nan
        return self._freqs, p, dur, gain


def parse_info(payload: bytes) -> dict[str, str]:
    info = {}
    for line in payload.decode("utf-8", "replace").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            info[k.strip()] = v.strip()
    return info


class E310Backend(SdrBackend):
    name = "E310"
    f_min_mhz = 70.0
    f_max_mhz = 6000.0
    params = [
        Param("host", "Adresse", "str", "192.168.10.11"),
        Param("port", "Port", "int", 7355, 1, 65535, 1),
        Param("rate", "Abtastrate", "float", 30.72, 2.1, 30.72, 0.5, live=True, suffix=" MS/s"),
        Param("gain_mode", "Verstärkung", "choice", "manual",
              choices=("manual", "slow_attack", "fast_attack", "hybrid"), live=True),
        Param("gain", "RX-Gain", "float", 50.0, -3.0, 71.0, 1.0, live=True, suffix=" dB"),
        Param("fft_size", "FFT-Größe", "choice", 1024, choices=(256, 512, 1024, 2048, 4096), live=True),
        Param("averages", "Mittelungen", "int", 16, 1, 512, 1, live=True),
        Param("usable", "Genutzte Bandbreite", "float", 0.75, 0.3, 0.95, 0.05, live=True, suffix=" ×Rate"),
        Param("settle", "Einschwingzeit", "float", 0.1, 0.0, 20.0, 0.1, live=True, suffix=" ms"),
        Param("max_rate", "Max. Sweeps/s (0 = frei)", "int", 25, 0, 500, 5, live=True),
        # Die Anzeige rastert auf 1500 Punkte; mehr Werte als nötig nicht übertragen (Max-Pooling im Gerät)
        Param("max_bins", "Max. Werte pro Sweep", "int", 4096, 500, 65536, 500, live=True),
        Param("rf_port", "RF-Port (leer = unverändert)", "str", ""),
    ]

    def __init__(self, settings: dict | None = None):
        super().__init__(settings)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.info: dict[str, str] = {}

    # -- Einstellungen -> Protokoll -------------------------------------------
    def _set_args(self, keys) -> list[str]:
        s = self.settings
        out = []
        for k in keys:
            v = s.get(k)
            if k == "rate":
                out.append(f"rate={float(v):.6g}")
            elif k == "gain_mode":
                out.append(f"gain_mode={v}")
            elif k == "gain":
                out.append(f"gain={float(v):.0f}")
            elif k == "fft_size":
                out.append(f"fft={int(v)}")
            elif k == "averages":
                out.append(f"avg={int(v)}")
            elif k == "usable":
                out.append(f"usable={float(v):.3f}")
            elif k == "settle":
                out.append(f"settle={float(v) * 1000:.0f}")
            elif k == "max_rate":
                out.append(f"max_rate={int(v)}")
            elif k == "max_bins":
                out.append(f"max_bins={int(v)}")
            elif k == "rf_port" and str(v).strip():
                out.append(f"port={str(v).strip()}")
        return out

    def _send(self, line: str) -> None:
        sock = self._sock
        if sock is None:
            return
        with self._send_lock:
            try:
                sock.sendall(line.encode() + b"\n")
            except OSError:
                pass        # Verbindungsabbruch meldet der Empfangs-Thread

    def update_setting(self, key: str, value) -> bool:
        live = super().update_setting(key, value)
        if live:
            self.take_pending()
            args = self._set_args([key])
            if args:
                self._send("set " + " ".join(args))
        return live

    # -- Thread ---------------------------------------------------------------
    def run(self) -> None:
        host = str(self.settings["host"]).strip()
        port = int(self.settings["port"])
        if not host:
            raise RuntimeError("keine Adresse eingestellt")
        self.state = f"Verbinde mit {host}:{port} …"
        try:
            sock = socket.create_connection((host, port), timeout=3.0)
        except OSError as exc:
            raise RuntimeError(f"{host}:{port} nicht erreichbar ({exc}) – läuft e310d auf dem Gerät? "
                               "(helper/e310: make install)") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.2)
        self._sock = sock
        parser = FrameParser()
        dec = SweepDecoder()
        try:
            args = self._set_args([p.key for p in self.params])
            self._send("set " + " ".join(args))
            lo, hi = self.range
            self._send(f"start {lo:.6f} {hi:.6f}")
            last_data = time.monotonic()
            while not self.stopped:
                try:
                    chunk = sock.recv(1 << 16)
                except socket.timeout:
                    if time.monotonic() - last_data > 5.0:
                        self.state = "Keine Daten vom E310 seit über 5 s"
                    continue
                except OSError as exc:
                    if self.stopped:
                        break
                    raise RuntimeError(f"Verbindung zum E310 unterbrochen ({exc})") from exc
                if not chunk:
                    if self.stopped:
                        break
                    raise RuntimeError("E310 hat die Verbindung beendet")
                last_data = time.monotonic()
                for typ, payload in parser.feed(chunk):
                    if typ == T_SWEEP:
                        f, p, dur, _gain = dec.decode(payload)
                        if len(f) == 0 or f[0] < lo - 1.0 or f[-1] > hi + 1.0:
                            continue        # Sweep eines früheren Bereichs
                        if self.state.startswith(("Verbinde", "Keine Daten")):
                            self.state = ""
                        self._emit(Sweep(f, p, lo, hi, time.time(), dur))
                    elif typ == T_INFO:
                        self._on_info(parse_info(payload))
                    elif typ == T_STATE:
                        self.state = payload.decode("utf-8", "replace")
                    elif typ == T_ERROR:
                        self.errors.put(f"{self.name}: {payload.decode('utf-8', 'replace')}")
        finally:
            self._send("stop")
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _on_info(self, info: dict[str, str]) -> None:
        self.info = info
        try:
            self.f_min_mhz = float(info.get("fmin_mhz", self.f_min_mhz))
            self.f_max_mhz = float(info.get("fmax_mhz", self.f_max_mhz))
        except ValueError:
            pass

    def _shutdown_hook(self) -> None:
        sock = self._sock
        if sock is not None:
            self._send("stop")
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
