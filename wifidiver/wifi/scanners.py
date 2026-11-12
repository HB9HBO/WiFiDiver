"""WLAN-Datenquellen: iw (Kernel-Scan-Cache), nmcli und Demo."""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import time

from . import oui
from .channels import channel_to_freq
from .iw_parser import parse_scan_dump
from .model import AccessPoint


def wireless_interfaces() -> list[str]:
    out = []
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            base = f"/sys/class/net/{name}"
            if os.path.exists(f"{base}/wireless") or os.path.exists(f"{base}/phy80211"):
                out.append(name)
    except OSError:
        pass
    return out


def _iw_bin() -> str | None:
    return shutil.which("iw") or next((p for p in ("/sbin/iw", "/usr/sbin/iw") if os.path.exists(p)), None)


def _nm_running() -> bool:
    if not shutil.which("nmcli"):
        return False
    try:
        r = subprocess.run(["nmcli", "-t", "-f", "RUNNING", "general"],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and "running" in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _iface_up(iface: str) -> bool:
    try:
        with open(f"/sys/class/net/{iface}/flags") as fh:
            return bool(int(fh.read().strip(), 16) & 0x1)
    except (OSError, ValueError):
        return True


class Scanner:
    name = "basis"

    def __init__(self, iface: str | None = None):
        self.iface = iface
        self.status = ""

    def scan(self) -> list[AccessPoint]:
        raise NotImplementedError


class IwScanner(Scanner):
    """Liest den Kernel-Scan-Cache (`iw scan dump`, ohne root).

    Neue Scans werden über NetworkManager ausgelöst, falls er läuft, sonst
    über `iw scan trigger` (benötigt CAP_NET_ADMIN).
    """

    name = "iw"

    def __init__(self, iface: str | None = None):
        super().__init__(iface)
        self.iw = _iw_bin()
        self.use_nm = _nm_running()
        self.trigger_ok = True

    def _trigger(self) -> None:
        if self.use_nm:
            subprocess.run(["nmcli", "dev", "wifi", "rescan", "ifname", self.iface],
                           capture_output=True, timeout=5)
            return
        if not self.trigger_ok:
            return
        r = subprocess.run([self.iw, "dev", self.iface, "scan", "trigger"],
                           capture_output=True, text=True, timeout=5)
        err = (r.stderr or "").strip()
        if r.returncode != 0 and "busy" not in err.lower():
            if re.search(r"\(-1\)", err) or "not permitted" in err.lower():
                self.trigger_ok = False
                self.status = ("Scan-Trigger braucht CAP_NET_ADMIN – nur Kernel-Cache wird gelesen "
                               "(siehe README)")
            else:
                self.status = f"iw scan trigger: {err}"
        elif r.returncode == 0:
            self.status = ""

    def scan(self) -> list[AccessPoint]:
        if not self.iw:
            raise RuntimeError("iw nicht gefunden")
        if not self.iface:
            raise RuntimeError("kein WLAN-Interface gefunden")
        if not _iface_up(self.iface):
            self.status = (f"{self.iface} ist down – z.B. 'sudo ip link set {self.iface} up' "
                           "(ohne NetworkManager)")
        try:
            self._trigger()
        except (OSError, subprocess.SubprocessError):
            pass
        r = subprocess.run([self.iw, "dev", self.iface, "scan", "dump"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "iw scan dump fehlgeschlagen")
        aps = parse_scan_dump(r.stdout)
        for ap in aps:
            ap.vendor = oui.lookup(ap.bssid)
        return aps


_NM_SPLIT = re.compile(r"(?<!\\):")


class NmcliScanner(Scanner):
    name = "nmcli"
    FIELDS_FULL = "IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY,BANDWIDTH"
    FIELDS_MIN = "IN-USE,SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY"

    def __init__(self, iface: str | None = None):
        super().__init__(iface)
        self.fields = self.FIELDS_FULL

    def _run(self, fields: str) -> subprocess.CompletedProcess:
        cmd = ["nmcli", "-t", "-e", "yes", "-f", fields, "dev", "wifi", "list", "--rescan", "yes"]
        if self.iface:
            cmd += ["ifname", self.iface]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)

    def scan(self) -> list[AccessPoint]:
        r = self._run(self.fields)
        if r.returncode != 0 and self.fields == self.FIELDS_FULL:
            self.fields = self.FIELDS_MIN
            r = self._run(self.fields)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "nmcli fehlgeschlagen")
        aps = []
        for line in r.stdout.splitlines():
            parts = [p.replace("\\:", ":").replace("\\\\", "\\") for p in _NM_SPLIT.split(line)]
            if len(parts) < 7:
                continue
            inuse, ssid, bssid, chan, freq, sig, sec = parts[:7]
            try:
                f = float(freq.split()[0])
                pct = float(sig)
            except (ValueError, IndexError):
                continue
            width = 20
            if len(parts) > 7:
                m = re.match(r"(\d+)", parts[7])
                if m:
                    width = int(m.group(1))
            ap = AccessPoint(bssid=bssid.lower(), ssid=ssid, freq=f,
                             signal=pct / 2.0 - 100.0, width=width,
                             security=sec or "Offen", connected=inuse.strip() == "*")
            ap.vendor = oui.lookup(ap.bssid)
            aps.append(ap.finalize())
        return aps


class DemoScanner(Scanner):
    """Simulierte Umgebung zum Testen ohne WLAN-Hardware."""

    name = "demo"

    _SPEC = [
        # ssid, bssid, band, kanal, breite, sig, sicherheit, gen
        ("Heimnetz", "a4:2b:b0:11:22:01", "2.4", 1, 20, -48, "WPA2/WPA3-Personal", "Wi-Fi 6"),
        ("Heimnetz", "a4:2b:b0:11:22:02", "5", 36, 80, -55, "WPA2/WPA3-Personal", "Wi-Fi 6"),
        ("Heimnetz", "a4:2b:b0:11:22:03", "6", 37, 160, -63, "WPA3-Personal", "Wi-Fi 6"),
        ("Nachbar-WLAN", "3c:84:6a:aa:bb:01", "2.4", 6, 20, -67, "WPA2-Personal", "Wi-Fi 4"),
        ("FRITZ!Box 7590", "e0:28:6d:01:02:03", "2.4", 11, 20, -72, "WPA2-Personal", "Wi-Fi 5"),
        ("FRITZ!Box 7590", "e0:28:6d:01:02:04", "5", 100, 80, -70, "WPA2-Personal", "Wi-Fi 5"),
        ("Gast", "a6:2b:b0:11:22:05", "5", 36, 80, -56, "OWE", "Wi-Fi 6"),
        ("", "16:91:82:33:44:55", "2.4", 6, 40, -80, "WPA2-Personal", "Wi-Fi 4"),
        ("Buero-5G", "00:1a:1e:12:34:56", "5", 149, 40, -76, "WPA2-Enterprise", "Wi-Fi 5"),
        ("Kamera", "b8:27:eb:de:ad:01", "2.4", 3, 20, -84, "WPA2-Personal", "Wi-Fi 4"),
        ("Labor-7", "00:0c:e6:77:00:01", "6", 69, 320, -71, "WPA3-Personal", "Wi-Fi 7"),
        ("Hotspot", "da:a1:19:00:00:01", "5", 52, 20, -81, "WPA2-Personal", "Wi-Fi 6"),
    ]

    def __init__(self, iface: str | None = None):
        super().__init__(iface)
        self._drift = {s[1]: 0.0 for s in self._SPEC}

    def scan(self) -> list[AccessPoint]:
        time.sleep(0.2)
        aps = []
        for ssid, bssid, band, ch, width, sig, sec, gen in self._SPEC:
            d = self._drift[bssid] = max(-8.0, min(8.0, self._drift[bssid] + random.gauss(0, 1.2)))
            ap = AccessPoint(bssid=bssid, ssid=ssid, freq=channel_to_freq(ch, band),
                             signal=round(sig + d, 1), width=width, security=sec,
                             generation=gen, utilization=random.uniform(2, 60),
                             stations=random.randint(0, 12), last_seen_ms=random.randint(50, 4000),
                             connected=bssid.endswith("22:02"))
            ap.vendor = oui.lookup(bssid)
            aps.append(ap.finalize())
        return aps


SCANNERS = {"iw": IwScanner, "nmcli": NmcliScanner, "demo": DemoScanner}


def auto_scanner(iface: str | None) -> Scanner:
    ifaces = wireless_interfaces()
    if iface is None and ifaces:
        iface = ifaces[0]
    if iface and _iw_bin():
        return IwScanner(iface)
    if _nm_running():
        return NmcliScanner(iface)
    s = DemoScanner()
    s.status = "Kein WLAN-Interface gefunden – Demo-Daten"
    return s
