"""USRP (UHD) Backend: schrittweises Abstimmen und Zusammensetzen der Teilspektren.

Das Gerät läuft in einem eigenen Prozess: Die UHD-Python-Bindings geben den GIL beim
Umstimmen (B2xx/AD9361: ~30 ms pro Schritt) nicht frei. Im selben Prozess würde der
GUI-Thread bei vielen Schritten pro Sweep (5 GHz: ~25) kaum noch drankommen und der
Wasserfall scheinbar stehen bleiben.
"""

from __future__ import annotations

import math
import multiprocessing as mp
import queue
import time

import numpy as np

from .base import Param, SdrBackend, Sweep, psd_dbfs

try:
    import uhd  # type: ignore
except ImportError:  # pragma: no cover - abhängig von der Installation
    uhd = None


_IMAGE_DIRS = ("/usr/share/uhd/images", "/usr/local/share/uhd/images", "/opt/uhd/share/uhd/images")


def find_images_dir() -> str | None:
    """FPGA-/Firmware-Images suchen (Debian legt sie versioniert ab: /usr/share/uhd/<ver>/images)."""
    import glob
    import os
    cands = list(_IMAGE_DIRS)
    for base in ("/usr/share/uhd", "/usr/local/share/uhd"):
        cands += sorted(glob.glob(f"{base}/*/images"), reverse=True)
    for d in cands:
        if glob.glob(os.path.join(d, "usrp_*")):
            return d
    return None


def ensure_images_dir() -> None:
    """UHD_IMAGES_DIR setzen, falls nicht vorhanden (z.B. weil sudo die Umgebung zurücksetzt).

    Muss vor dem Öffnen des Geräts laufen; der Kindprozess erbt die Umgebung.
    """
    import os
    cur = os.environ.get("UHD_IMAGES_DIR")
    if cur and os.path.isdir(cur):
        return
    d = find_images_dir()
    if d:
        os.environ["UHD_IMAGES_DIR"] = d


# ---------------------------------------------------------------------------
# Kindprozess
# ---------------------------------------------------------------------------
class _UhdSweeper:
    """Läuft im Kindprozess; hält das Gerät über mehrere Sweep-Läufe offen."""

    def __init__(self, ctrl, out):
        self.ctrl = ctrl
        self.out = out
        self.usrp = None
        self.streamer = None
        self.dev_args = None
        self.settings: dict = {}
        self.range = (0.0, 0.0)
        self.active = False
        self.restart = False
        self.quit = False
        self.tuned_fc = None

    # -- Kommandos ----------------------------------------------------------
    def _handle(self, msg) -> None:
        cmd = msg[0]
        if cmd == "start":
            _, lo, hi, settings = msg
            self.range = (lo, hi)
            self.settings = dict(settings)
            self.active = True
            self.restart = True
        elif cmd == "stop":
            self.active = False
            self.out.put(("stopped",))
        elif cmd == "set":
            _, key, val = msg
            self.settings[key] = val
            if self.usrp is not None:
                if key == "gain":
                    self.usrp.set_rx_gain(float(val), 0)
                elif key == "antenna" and val in self.usrp.get_rx_antennas(0):
                    self.usrp.set_rx_antenna(val, 0)
        elif cmd == "quit":
            self.active = False
            self.quit = True

    def _poll(self, block: bool = False) -> None:
        try:
            msg = self.ctrl.get(timeout=0.5) if block else self.ctrl.get_nowait()
        except queue.Empty:
            return
        self._handle(msg)
        while True:
            try:
                self._handle(self.ctrl.get_nowait())
            except queue.Empty:
                return

    # -- Gerät ----------------------------------------------------------------
    def _open(self) -> None:
        s = self.settings
        if self.usrp is None or self.dev_args != s["args"]:
            self.out.put(("state", "Öffne USRP … (erstes Öffnen lädt das FPGA, über USB 2 bis zu ~1–2 min)"))
            self.streamer = None
            self.usrp = None
            try:
                self.usrp = uhd.usrp.MultiUSRP(s["args"])
            except (RuntimeError, LookupError) as exc:
                import os
                img = os.environ.get("UHD_IMAGES_DIR") or "nicht gefunden"
                raise RuntimeError(f"USRP nicht gefunden ({exc}) – UHD_IMAGES_DIR: {img}") from exc
            self.dev_args = s["args"]
            fr = self.usrp.get_rx_freq_range(0)
            self.out.put(("info", fr.start() / 1e6, fr.stop() / 1e6, tuple(self.usrp.get_rx_antennas(0))))
            self.out.put(("state", ""))
        u = self.usrp
        u.set_rx_rate(float(s["rate"]) * 1e6, 0)
        self.tuned_fc = None
        self.rate = u.get_rx_rate(0)
        try:
            u.set_rx_bandwidth(min(self.rate, 56e6), 0)
        except RuntimeError:
            pass
        u.set_rx_gain(float(s["gain"]), 0)
        if s["antenna"] in u.get_rx_antennas(0):
            u.set_rx_antenna(s["antenna"], 0)
        if self.streamer is None:
            st_args = uhd.usrp.StreamArgs("fc32", "sc16")
            st_args.channels = [0]
            self.streamer = u.get_rx_stream(st_args)

    def _flush(self, buf: np.ndarray) -> None:
        md = uhd.types.RXMetadata()
        self.streamer.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
        while self.streamer.recv(buf, md, 0.05):
            pass

    def _capture(self, fc_hz: float, nsamps: int, discard: int, buf: np.ndarray) -> np.ndarray | None:
        if fc_hz != self.tuned_fc:
            self.usrp.set_rx_freq(uhd.types.TuneRequest(fc_hz), 0)
            self.tuned_fc = fc_hz
        else:
            discard = 0          # gleiche Frequenz: kein Einschwingen nötig
        total = nsamps + discard
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps = total
        cmd.stream_now = True
        self.streamer.issue_stream_cmd(cmd)
        md = uhd.types.RXMetadata()
        got = 0
        ok = True
        while got < total:
            n = self.streamer.recv(buf[:, got:total], md, 0.5)
            ec = md.error_code
            if ec == uhd.types.RXMetadataErrorCode.timeout:
                ok = False
                break
            if ec != uhd.types.RXMetadataErrorCode.none:
                ok = False
                if ec != uhd.types.RXMetadataErrorCode.overflow:
                    break
            got += n
            if md.end_of_burst:
                break
        if not ok or got < total:
            # Rest des Bursts verwerfen, damit der nächste Schritt sauber startet
            self._flush(buf)
            self.tuned_fc = None
            return None
        return buf[0, discard:total]

    def _sweep_loop(self) -> None:
        """Sweept bis Stop/Neustart; kehrt zurück, wenn ein neues Kommando das verlangt."""
        self.restart = False
        self._open()
        lo, hi = self.range
        fr = self.usrp.get_rx_freq_range(0)
        lo = max(lo, fr.start() / 1e6)
        hi = min(hi, fr.stop() / 1e6)
        if hi <= lo:
            raise RuntimeError("Frequenzbereich außerhalb des Geräts")

        fft = int(self.settings["fft_size"])
        window = np.blackman(fft).astype(np.float32)
        span = self.rate * float(self.settings["usable"]) / 1e6       # MHz pro Schritt
        n_steps = max(1, math.ceil((hi - lo) / span))
        centers = lo + span * (np.arange(n_steps) + 0.5)
        if n_steps == 1:
            # schmaler Bereich: DC-Spitze (Abstimmfrequenz) wenn möglich knapp unterhalb ablegen
            fc = lo - 1.0 if hi - (lo - 1.0) <= span / 2 else (lo + hi) / 2
            centers = np.array([fc])
        bin_mhz = self.rate / 1e6 / fft
        rel = (np.arange(fft) - fft // 2) * bin_mhz                    # Offset der Bins (MHz)
        keep = np.abs(rel) <= span / 2 + bin_mhz / 2
        dc = max(1, fft // 512)
        dc_idx = np.arange(fft // 2 - dc, fft // 2 + dc + 1)

        max_samps = fft * 512 + int(self.rate * 0.02) + 4096
        buf = np.zeros((1, max_samps), dtype=np.complex64)
        fails = 0

        while self.active and not self.restart and not self.quit:
            t0 = time.time()
            avg = int(self.settings["averages"])
            discard = int(self.rate * float(self.settings["settle"]) / 1e3)
            nsamps = fft * avg
            fs, ps = [], []
            for fc in centers:
                self._poll()
                if not self.active or self.restart or self.quit:
                    return
                samples = None
                for _ in range(3):
                    samples = self._capture(fc * 1e6, nsamps, discard, buf)
                    if samples is not None:
                        break
                if samples is None:
                    continue
                p = psd_dbfs(samples, fft, window)
                # DC-Spitze durch Interpolation der Nachbarn ersetzen
                left, right = p[dc_idx[0] - 1], p[dc_idx[-1] + 1]
                p[dc_idx] = np.linspace(left, right, len(dc_idx))
                f = fc + rel
                m = keep & (f >= lo) & (f <= hi)
                fs.append(f[m])
                ps.append(p[m].astype(np.float32))
            if fs:
                fails = 0
                now = time.time()
                try:   # GUI kommt nicht nach: Sweep verwerfen statt blockieren
                    self.out.put(("sweep", np.concatenate(fs), np.concatenate(ps), lo, hi, now, now - t0),
                                 timeout=0.2)
                except queue.Full:
                    pass
                # optional begrenzen: spart CPU (USB, UHD) auf schwachen Rechnern
                max_rate = float(self.settings.get("max_rate", 0) or 0)
                if max_rate > 0:
                    rest = t0 + 1.0 / max_rate - time.time()
                    if rest > 0:
                        time.sleep(min(rest, 1.0))
            else:
                fails += 1
                self.out.put(("state", f"Keine Daten vom USRP ({fails} Sweeps ohne Samples) – "
                                       "Streamer wird neu aufgebaut"))
                self.streamer = None
                self._open()

    def serve(self) -> None:
        while not self.quit:
            if not self.active:
                self._poll(block=True)
                continue
            try:
                self._sweep_loop()
            except Exception as exc:  # noqa: BLE001 - an die GUI melden
                self.active = False
                self.out.put(("error", str(exc)))


def _worker(ctrl, out) -> None:
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Strg+C beendet nur die GUI, die räumt auf
    if uhd is None:
        out.put(("error", "Python-Modul 'uhd' nicht installiert"))
        return
    _UhdSweeper(ctrl, out).serve()


# ---------------------------------------------------------------------------
# GUI-Seite
# ---------------------------------------------------------------------------
class UHDBackend(SdrBackend):
    name = "UHD"
    f_min_mhz = 10.0
    f_max_mhz = 6000.0
    params = [
        Param("args", "Geräte-Args", "str", ""),
        Param("rate", "Abtastrate", "float", 56.0, 1.0, 61.44, 0.5, suffix=" MS/s"),
        Param("gain", "RX-Gain", "float", 50.0, 0.0, 76.0, 1.0, live=True, suffix=" dB"),
        Param("antenna", "Antenne", "choice", "RX2", choices=("RX2", "TX/RX"), live=True),
        Param("fft_size", "FFT-Größe", "choice", 1024, choices=(256, 512, 1024, 2048, 4096)),
        Param("averages", "Mittelungen", "int", 16, 1, 512, 1, live=True),
        Param("usable", "Genutzte Bandbreite", "float", 0.75, 0.3, 0.95, 0.05, suffix=" ×Rate"),
        Param("settle", "Einschwingzeit", "float", 0.5, 0.0, 20.0, 0.1, live=True, suffix=" ms"),
        # Standard 25 = Standard-Bildrate: schnellere Sweeps werden ohnehin zu einer Wasserfallzeile
        # zusammengefasst; ungebremst (~75/s) kostet der USRP etwa dreimal so viel CPU
        Param("max_rate", "Max. Sweeps/s (0 = frei)", "int", 25, 0, 500, 5, live=True),
    ]

    def __init__(self, settings: dict | None = None):
        super().__init__(settings)
        self._proc = None
        self._ctrl = None
        self._out = None

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if uhd is None:
            return False, "Python-Modul 'uhd' nicht installiert (Paket python3-uhd)"
        return True, ""

    def _ensure_proc(self) -> bool:
        """Startet den Geräteprozess bei Bedarf; True, wenn er neu gestartet wurde."""
        if self._proc is not None and self._proc.is_alive():
            return False
        ensure_images_dir()
        ctx = mp.get_context("spawn")      # kein fork: Qt/UHD-Zustand nicht in den Kindprozess
        self._ctrl = ctx.Queue()
        self._out = ctx.Queue(maxsize=64)
        self._proc = ctx.Process(target=_worker, args=(self._ctrl, self._out),
                                 name="wifidiver-uhd", daemon=True)
        self._proc.start()
        return True

    def update_setting(self, key: str, value) -> bool:
        live = super().update_setting(key, value)
        if live:
            self.take_pending()
            if self._ctrl is not None and self._proc is not None and self._proc.is_alive():
                self._ctrl.put(("set", key, value))
        return live

    def run(self) -> None:
        new = self._ensure_proc()
        out = self._out
        # alte Meldungen eines vorherigen Laufs verwerfen
        while True:
            try:
                out.get_nowait()
            except queue.Empty:
                break
        self._ctrl.put(("start", self.range[0], self.range[1], dict(self.settings)))
        if new:
            self.state = "Starte USRP-Prozess …"
        while not self.stopped:
            try:
                msg = out.get(timeout=0.2)
            except queue.Empty:
                if not self._proc.is_alive():
                    raise RuntimeError(f"USRP-Prozess beendet (Exitcode {self._proc.exitcode})")
                continue
            kind = msg[0]
            if kind == "sweep":
                _, f, p, lo, hi, ts, dur = msg
                self.state = ""
                self._emit(Sweep(f, p, lo, hi, ts, dur))
            elif kind == "state":
                self.state = msg[1]
            elif kind == "info":
                _, fmin, fmax, ants = msg
                self.f_min_mhz, self.f_max_mhz = fmin, fmax
                if ants:
                    for prm in self.params:
                        if prm.key == "antenna":
                            prm.choices = ants
            elif kind == "error":
                raise RuntimeError(msg[1])

    def _shutdown_hook(self) -> None:
        if self._ctrl is not None and self._proc is not None and self._proc.is_alive():
            self._ctrl.put(("stop",))

    def close(self) -> None:
        self.stop()
        p = self._proc
        if p is not None:
            if p.is_alive():
                self._ctrl.put(("quit",))
                p.join(timeout=3)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)
        self._proc = None
