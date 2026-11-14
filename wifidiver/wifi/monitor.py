"""WLAN-Monitormodus: Hilfsprozess starten, Frames auswerten, Netze/Clients/Kanalauslastung sammeln.

Aufbau:
    MonitorBackend (Oberfläche)  ──Queue──  _worker (eigener Prozess, wertet aus)
                                               └─ wifidiver-monitor (C, mit Capabilities)
Die Oberfläche bekommt zweimal pro Sekunde einen Schnappschuss (reine Python-Daten).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import re
import select
import struct
import subprocess
import time
from pathlib import Path

from . import oui
from .channels import band_of_freq, freq_to_channel
from .ieee80211 import airtime_us, is_random_mac, is_unicast, mac, parse_beacon_body, parse_radiotap
from .model import AccessPoint
from .threats import ThreatDetector

HELPER_NAME = "wifidiver-monitor"
INSTALLED = Path("/usr/local/lib/wifidiver") / HELPER_NAME
IN_REPO = Path(__file__).resolve().parents[2] / "helper" / HELPER_NAME

R_FRAME, R_CHANNEL, R_MSG, R_READY, R_STATS = 1, 2, 3, 4, 5
_HDR = struct.Struct("<BBHIIIQ")
MODES = {0: "vorhandenes Monitor-Interface", 1: "zusätzliches Monitor-Interface",
         2: "Interface in den Monitormodus umgeschaltet"}

EXPIRE_S = 300.0          # Netze/Clients ohne Lebenszeichen danach vergessen
SNAPSHOT_S = 0.5


# ----------------------------------------------------------------------------- Hilfsprogramm
def find_helper() -> Path | None:
    env = os.environ.get("WIFIDIVER_MONITOR")
    for p in ([Path(env)] if env else []) + [INSTALLED, IN_REPO]:
        if p.is_file():
            return p
    return None


def _has_caps(path: Path) -> bool:
    try:
        return bool(os.getxattr(path, "security.capability"))
    except OSError:
        return False


def helper_status() -> tuple[Path | None, str]:
    """(Pfad, Fehlertext); Fehlertext leer, wenn startbereit."""
    p = find_helper()
    if p is None:
        return None, ("Hilfsprogramm fehlt – einmalig: cd helper && make && sudo make install "
                      "(siehe README)")
    if not os.access(p, os.X_OK):
        return p, f"{p} ist nicht ausführbar (Gruppe netdev?)"
    if not _has_caps(p) and not os.environ.get("WIFIDIVER_MONITOR"):   # Variable: nur für Tests
        return p, (f"{p} hat keine Capabilities – einmalig: cd helper && sudo make install")
    return p, ""


def allowed_freqs(iface: str) -> list[int]:
    """Frequenzen, die der Adapter laut Regulierung nutzen darf (ohne „disabled“)."""
    try:
        phy = Path(f"/sys/class/net/{iface}/phy80211/name").read_text().strip()
    except OSError:
        return []
    iw = next((p for p in ("/usr/sbin/iw", "/sbin/iw", "/usr/bin/iw") if os.path.exists(p)), "iw")
    try:
        out = subprocess.run([iw, "phy", phy, "info"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    freqs = []
    for m in re.finditer(r"^\s*\* (\d+)(?:\.\d+)? MHz \[\d+\](.*)$", out, re.M):
        if "disabled" not in m.group(2):
            freqs.append(int(m.group(1)))
    return sorted(set(f for f in freqs if band_of_freq(f)))


# ----------------------------------------------------------------------------- Auswertung
class _Bss:
    __slots__ = ("ssid", "hidden", "info", "info_t", "signal", "sig_n", "beacons", "frames",
                 "retries", "first", "last", "deauth", "tuned")

    def __init__(self, now):
        self.ssid = ""
        self.hidden = False
        self.info: dict | None = None
        self.info_t = 0.0
        self.signal: float | None = None
        self.sig_n = 0
        self.beacons = 0
        self.frames = 0
        self.retries = 0
        self.first = now
        self.last = now
        self.deauth = 0
        self.tuned = 0


class _Sta:
    __slots__ = ("bssid", "signal", "frames", "bytes", "retries", "first", "last", "probes", "freq")

    def __init__(self, now):
        self.bssid: str | None = None
        self.signal: float | None = None
        self.frames = 0
        self.bytes = 0
        self.retries = 0
        self.first = now
        self.last = now
        self.probes: set[str] = set()
        self.freq = 0


class _Chan:
    __slots__ = ("busy", "busy_last", "fps", "retry", "observed", "frames_tot", "last")

    def __init__(self):
        self.busy: float | None = None      # gleitender Mittelwert in %
        self.busy_last = 0.0
        self.fps = 0.0
        self.retry = 0.0
        self.observed = 0.0                 # Sekunden auf diesem Kanal
        self.frames_tot = 0
        self.last = 0.0


def _ema(old, new, a):
    return new if old is None else old + a * (new - old)


class MonitorStats:
    """Wertet die Datensätze des Hilfsprogramms aus (testbar ohne Hardware)."""

    def __init__(self):
        self.bss: dict[str, _Bss] = {}
        self.sta: dict[str, _Sta] = {}
        self.chan: dict[int, _Chan] = {}
        self.freq = 0
        self.ready: tuple[str, str] | None = None
        self.messages: list[str] = []
        self.drops = 0
        self.frames = 0
        self.beacons = 0
        self.bad = 0
        self.hops = 0
        self._win_start = 0          # ns (CLOCK_MONOTONIC des Hilfsprogramms)
        self._win_busy = 0.0
        self._win_frames = 0
        self._win_retry = 0
        self._ampdu = None
        self._cycle_first = 0
        self._cycle_t0 = 0
        self.cycle_s = 0.0
        self.samples: list[tuple[float, str, float]] = []    # (Wanduhr, BSSID, dBm)
        self._sample_t: dict[str, float] = {}
        self._clock_off = time.time() - time.monotonic()
        self.threats = ThreatDetector(self)

    # -- Datensätze ---------------------------------------------------------------
    def feed(self, buf) -> int:
        """Verarbeitet vollständige Datensätze aus buf; gibt die Anzahl verbrauchter Bytes zurück."""
        off, n = 0, len(buf)
        mv = memoryview(buf)
        hs = _HDR.size
        while off + hs <= n:
            typ, _fl, caplen, freq, origlen, aux, ts = _HDR.unpack_from(buf, off)
            end = off + hs + caplen
            if end > n:
                break
            if typ == R_FRAME:
                self._frame(bytes(mv[off + hs:end]), freq, origlen, ts)
            elif typ == R_CHANNEL:
                self._close_window(ts)
                if self._cycle_first == 0 or freq == self._cycle_first:
                    if self._cycle_first and self._cycle_t0:
                        self.cycle_s = (ts - self._cycle_t0) / 1e9
                    self._cycle_first = freq
                    self._cycle_t0 = ts
                self.freq = freq
                self.hops += 1
            elif typ == R_MSG:
                self.messages.append(bytes(mv[off + hs:end]).decode("utf-8", "replace"))
                del self.messages[:-8]
            elif typ == R_READY:
                name = bytes(mv[off + hs:end]).decode("ascii", "replace")
                self.ready = (name, MODES.get(origlen, ""))
                self._win_start = ts
            elif typ == R_STATS:
                self.drops += aux
            off = end
        return off

    def _close_window(self, ts: int) -> None:
        """Messfenster für den bisherigen Kanal abschließen (Kanalwechsel oder Zeitablauf)."""
        if self.freq and self._win_start and ts > self._win_start:
            dt = (ts - self._win_start) / 1e9
            if dt >= 0.03:
                c = self.chan.setdefault(self.freq, _Chan())
                pct = min(100.0, self._win_busy / (dt * 1e6) * 100.0)
                c.busy_last = pct
                c.busy = _ema(c.busy, pct, 0.35)
                c.fps = _ema(c.fps if c.observed else None, self._win_frames / dt, 0.35)
                r = self._win_retry / self._win_frames * 100.0 if self._win_frames else 0.0
                c.retry = _ema(c.retry if c.observed else None, r, 0.35)
                c.observed += dt
                c.frames_tot += self._win_frames
                c.last = ts / 1e9 + self._clock_off
        self._win_start = ts
        self._win_busy = 0.0
        self._win_frames = 0
        self._win_retry = 0
        self._ampdu = None

    def tick(self, now_ns: int) -> None:
        """Bei festem Kanal das Fenster jede Sekunde abschließen."""
        if self._win_start and now_ns - self._win_start > 1e9:
            self._close_window(now_ns)

    def _frame(self, b: bytes, freq: int, origlen: int, ts: int) -> None:
        rt = parse_radiotap(b)
        if rt is None or rt.flags & 0x40:          # kaputt bzw. FCS-Fehler
            self.bad += 1
            return
        o = rt.length
        if len(b) < o + 10:
            self.bad += 1
            return
        self.frames += 1
        flen = max(0, origlen - o)
        new_ppdu = rt.ampdu is None or rt.ampdu != self._ampdu
        self._ampdu = rt.ampdu
        self._win_busy += airtime_us(rt, flen, new_ppdu)
        self._win_frames += 1
        fc0, fc1 = b[o], b[o + 1]
        ftype, sub = (fc0 >> 2) & 3, (fc0 >> 4) & 15
        retry = bool(fc1 & 0x08)
        if retry:
            self._win_retry += 1
        now = ts / 1e9 + self._clock_off
        sig = rt.signal
        if ftype == 0:
            if len(b) < o + 24:
                return
            self._mgmt(b, o, sub, freq, sig, now, retry)
        elif ftype == 2:
            if len(b) < o + 24:
                return
            to_ds, from_ds = fc1 & 1, fc1 & 2
            a1, a2 = mac(b, o + 4), mac(b, o + 10)
            if to_ds and not from_ds:          # Client -> AP
                self._link(a2, a1, sig, now, flen, retry, freq, True)
            elif from_ds and not to_ds:        # AP -> Client
                bss = self._bss(a2, now)
                bss.frames += 1
                bss.retries += retry
                self._sig_bss(a2, bss, sig, now)
                if is_unicast(a1):
                    self._link(a1, a2, None, now, flen, False, freq, False)

    def _bss(self, bssid: str, now: float) -> _Bss:
        bss = self.bss.get(bssid)
        if bss is None:
            bss = self.bss[bssid] = _Bss(now)
        bss.last = now
        return bss

    def _sig_bss(self, bssid: str, bss: _Bss, sig, now: float) -> None:
        if sig is None or sig == 0:
            return
        bss.signal = _ema(bss.signal, float(sig), 0.3)
        bss.sig_n += 1
        if now - self._sample_t.get(bssid, 0.0) >= 0.5:
            self._sample_t[bssid] = now
            self.samples.append((now, bssid, round(bss.signal, 1)))

    def _link(self, sta_mac: str, bssid: str, sig, now, flen, retry, freq, from_sta) -> None:
        if not is_unicast(sta_mac) or sta_mac == bssid:
            return
        st = self.sta.get(sta_mac)
        if st is None:
            st = self.sta[sta_mac] = _Sta(now)
        st.last = now
        st.bssid = bssid
        st.freq = freq
        if from_sta:
            st.frames += 1
            st.bytes += flen
            st.retries += retry
            if sig:
                st.signal = _ema(st.signal, float(sig), 0.3)
        self._bss(bssid, now)

    def _mgmt(self, b: bytes, o: int, sub: int, freq: int, sig, now: float, retry: bool) -> None:
        a1, a2, a3 = mac(b, o + 4), mac(b, o + 10), mac(b, o + 16)
        if sub in (8, 5):                                  # Beacon / Probe Response
            new = a3 not in self.bss
            bss = self._bss(a3, now)
            bss.beacons += sub == 8
            self.beacons += 1
            bss.tuned = freq
            self._sig_bss(a3, bss, sig, now)
            if bss.info is None or now - bss.info_t > 5.0 or (sub == 5 and bss.hidden):
                info = parse_beacon_body(b[o + 24:], freq)
                if info is not None:
                    ssid = info["ssid"]
                    if ssid:
                        bss.ssid = ssid
                    elif sub == 8:
                        bss.hidden = True
                    bss.info = info
                    bss.info_t = now
            self.threats.beacon(b, o, a3, a2, now, new and sub == 8)
            if sub == 5:                                    # SSID direkt (erstes IE) für Karma
                p = o + 36
                if len(b) >= p + 2 and b[p] == 0 and 0 < b[p + 1] <= 32 and len(b) >= p + 2 + b[p + 1]:
                    self.threats.probe_response(a2, b[p + 2:p + 2 + b[p + 1]].decode("utf-8", "replace"),
                                                freq, now)
        elif sub == 4:                                      # Probe Request
            if not is_unicast(a2):
                return
            st = self.sta.get(a2)
            if st is None:
                st = self.sta[a2] = _Sta(now)
            st.last = now
            st.freq = freq
            if sig:
                st.signal = _ema(st.signal, float(sig), 0.3)
            info = b[o + 24:]
            if len(info) >= 2 and info[0] == 0 and 0 < info[1] <= 32 and len(info) >= 2 + info[1]:
                st.probes.add(info[2:2 + info[1]].decode("utf-8", "replace"))
        elif sub in (0, 2):                                 # (Re)Association Request
            self._link(a2, a3, sig, now, 0, retry, freq, True)
        elif sub in (10, 12):                               # Disassoc / Deauth
            bss = self.bss.get(a3)
            if bss is not None:
                bss.deauth += 1
            self.threats.deauth(b, o, sub, freq, sig, now)

    # -- Schnappschuss ------------------------------------------------------------
    def expire(self, now: float) -> None:
        for d in (self.bss, self.sta):
            for k in [k for k, v in d.items() if now - v.last > EXPIRE_S]:
                del d[k]

    def snapshot(self) -> dict:
        now = time.time()
        self.expire(now)
        clients: dict[str, int] = {}
        for st in self.sta.values():
            if st.bssid:
                clients[st.bssid] = clients.get(st.bssid, 0) + 1
        bss = {}
        for k, v in self.bss.items():
            bss[k] = {"ssid": v.ssid, "hidden": v.hidden, "info": v.info, "signal": v.signal,
                      "beacons": v.beacons, "frames": v.frames, "retries": v.retries,
                      "first": v.first, "last": v.last, "clients": clients.get(k, 0),
                      "deauth": v.deauth, "tuned": v.tuned, "sig_n": v.sig_n}
        sta = {}
        for k, v in self.sta.items():
            sta[k] = {"bssid": v.bssid, "signal": v.signal, "frames": v.frames, "bytes": v.bytes,
                      "retries": v.retries, "first": v.first, "last": v.last,
                      "probes": sorted(v.probes), "freq": v.freq, "random": is_random_mac(k)}
        chan = {f: {"busy": c.busy, "busy_last": c.busy_last, "fps": c.fps, "retry": c.retry,
                    "observed": c.observed, "last": c.last, "frames": c.frames_tot}
                for f, c in self.chan.items()}
        snap = {"t": now, "bss": bss, "sta": sta, "chan": chan, "freq": self.freq,
                "ready": self.ready, "messages": list(self.messages), "drops": self.drops,
                "frames": self.frames, "beacons": self.beacons, "bad": self.bad, "cycle_s": self.cycle_s,
                "samples": self.samples, "events": self.threats.snapshot(now),
                "threat_last": self.threats.last_hit}
        self.samples = []
        return snap


def ap_from_monitor(bssid: str, d: dict, now: float | None = None) -> AccessPoint | None:
    """AccessPoint für die Tabelle aus den Monitor-Daten eines Netzes."""
    info = d.get("info")
    if info is None or d.get("signal") is None:
        return None
    now = time.time() if now is None else now
    ap = AccessPoint(bssid=bssid, ssid=d.get("ssid") or "", freq=float(info["freq"]),
                     signal=float(d["signal"]), width=int(info["width"]),
                     center_freq=float(info["center"] or 0.0), security=info["security"],
                     generation=info["gen"], utilization=info["utilization"],
                     stations=info["stations"], ht_offset=info["ht_offset"],
                     last_seen_ms=int(max(0.0, now - d["last"]) * 1000))
    if "80+80" in info:
        ap.extra["80+80"] = info["80+80"]
    ap.vendor = oui.lookup(bssid)
    ap.extra["mon"] = d
    ap.extra["source"] = "monitor"
    return ap.finalize()


# ----------------------------------------------------------------------------- Worker-Prozess
def _worker(cmd_q, out_q, helper: str, iface: str, freqs: list[int], dwell: int) -> None:
    try:
        proc = subprocess.Popen([helper, iface], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
    except OSError as exc:
        out_q.put(("end", f"Hilfsprogramm nicht startbar: {exc}"))
        return

    def send(line: str) -> None:
        try:
            proc.stdin.write(line.encode() + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    send(f"D {int(dwell)}")
    send("F " + " ".join(str(int(f)) for f in freqs))
    stats = MonitorStats()
    buf = bytearray()
    fd = proc.stdout.fileno()
    last = 0.0
    reason = ""
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                chunk = os.read(fd, 1 << 18)
                if not chunk:
                    break
                buf += chunk
                used = stats.feed(buf)
                if used:
                    del buf[:used]
            try:
                while True:
                    cmd = cmd_q.get_nowait()
                    if cmd[0] == "stop":
                        send("Q")
                        reason = "gestoppt"
                        raise StopIteration
                    if cmd[0] == "hop":
                        send(f"D {int(cmd[2])}")
                        send("F " + " ".join(str(int(f)) for f in cmd[1]))
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last >= SNAPSHOT_S:
                last = now
                stats.tick(time.monotonic_ns())
                snap = stats.snapshot()
                try:
                    out_q.put_nowait(("snap", snap))
                except queue.Full:
                    pass
    except StopIteration:
        pass
    except Exception as exc:  # noqa: BLE001
        reason = f"Fehler in der Auswertung: {exc}"
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=3)
    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    if not reason or proc.returncode not in (0, None):
        reason = err.splitlines()[-1] if err else (stats.messages[-1] if stats.messages else
                                                     f"Hilfsprogramm beendet ({proc.returncode})")
    out_q.put(("end", reason))


class MonitorBackend:
    """Steuert den Worker-Prozess; wird aus dem GUI-Thread benutzt."""

    def __init__(self):
        self.proc: mp.Process | None = None
        self.cmd_q = self.out_q = None
        self.snapshot: dict | None = None
        self.error = ""
        self.iface = ""

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def start(self, iface: str, freqs: list[int], dwell: int) -> bool:
        self.stop()
        path, err = helper_status()
        if err:
            self.error = err
            return False
        if not freqs:
            self.error = "keine Kanäle ausgewählt"
            return False
        ctx = mp.get_context("spawn")
        self.cmd_q = ctx.Queue()
        self.out_q = ctx.Queue(maxsize=4)
        proc = ctx.Process(target=_worker, args=(self.cmd_q, self.out_q, str(path), iface,
                                                 list(freqs), int(dwell)), daemon=True)
        try:
            proc.start()
        except Exception as exc:  # noqa: BLE001
            self.error = f"Monitor-Prozess nicht startbar: {exc}"
            self.cmd_q = self.out_q = None
            return False
        self.proc = proc
        self.iface = iface
        self.error = ""
        self.snapshot = None
        return True

    def set_hop(self, freqs: list[int], dwell: int) -> None:
        if self.running and freqs:
            self.cmd_q.put(("hop", list(freqs), int(dwell)))

    def poll(self) -> bool:
        """Neue Daten abholen; True, wenn sich etwas geändert hat."""
        if self.out_q is None:
            return False
        changed = False
        samples = []
        try:
            while True:
                kind, data = self.out_q.get_nowait()
                if kind == "snap":
                    samples += data["samples"]
                    data["samples"] = samples
                    self.snapshot = data
                elif kind == "end":
                    self.error = data
                    self._reap()
                changed = True
        except queue.Empty:
            pass
        except (EOFError, OSError):
            self._reap()
            changed = True
        if self.proc is not None and not self.proc.is_alive() and self.out_q is not None:
            # Prozess weg, ohne „end“ zu melden
            try:
                kind, data = self.out_q.get(timeout=0.2)
                if kind == "end":
                    self.error = data
            except (queue.Empty, EOFError, OSError):
                self.error = self.error or "Monitor-Prozess beendet"
            self._reap()
            changed = True
        return changed

    def _reap(self) -> None:
        if self.proc is not None:
            self.proc.join(timeout=1)
        self.proc = None

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.cmd_q.put(("stop",))
            self.proc.join(timeout=5)
        except (OSError, ValueError):
            pass
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=3)
        try:
            while True:
                kind, data = self.out_q.get_nowait()
        except (queue.Empty, EOFError, OSError, ValueError):
            pass
        self.proc = None


def channel_label(freq: int) -> str:
    return f"{freq_to_channel(freq)}"
