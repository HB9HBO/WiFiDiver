"""Aktuelle WLAN-Verbindung (Verbindung-Tab).

Linux: `iw dev <if> link`, `iw dev <if> station dump` und `iw dev <if> survey dump` – alles ohne root.
Zähler (Bytes, Retries, Busy-Zeit) werden zwischen zwei Abfragen differenziert, damit aktuelle
Raten angezeigt werden statt Summen seit dem Verbindungsaufbau.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field

from . import oui
from .model import AccessPoint

Row = tuple[str, str]
Section = tuple[str, list[Row]]


@dataclass
class LinkInfo:
    iface: str
    connected: bool = False
    bssid: str = ""
    ssid: str = ""
    freq: float = 0.0
    link: dict = field(default_factory=dict)      # iw link
    station: dict = field(default_factory=dict)   # iw station dump (verbundener AP)
    survey: dict = field(default_factory=dict)    # iw survey dump ([in use])
    ts: float = field(default_factory=time.time)


def _kv(lines) -> dict:
    out = {}
    for line in lines:
        key, sep, val = line.strip().partition(":")
        if sep:
            out[key.strip().lower()] = val.strip()
    return out


def parse_link(text: str, iface: str) -> LinkInfo:
    info = LinkInfo(iface)
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Connected to"):
        return info
    m = re.match(r"Connected to ([0-9a-fA-F:]{17})", lines[0])
    info.connected = True
    info.bssid = m.group(1).lower() if m else ""
    info.link = _kv(lines[1:])
    info.ssid = info.link.get("ssid", "")
    try:
        info.freq = float(info.link.get("freq", "0").split()[0])
    except (ValueError, IndexError):
        pass
    return info


def parse_station_dump(text: str, bssid: str) -> dict:
    cur = None
    blocks: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"Station ([0-9a-fA-F:]{17})", line)
        if m:
            cur = m.group(1).lower()
            blocks[cur] = []
        elif cur:
            blocks[cur].append(line)
    if bssid in blocks:
        return _kv(blocks[bssid])
    return _kv(next(iter(blocks.values()))) if blocks else {}


def parse_survey(text: str) -> dict:
    """Werte des Kanals mit '[in use]'."""
    block: list[str] = []
    found = None
    for line in text.splitlines():
        if line.startswith("Survey data"):
            if found is None and block and any("[in use]" in l for l in block):
                found = block
            block = []
        else:
            block.append(line)
    if found is None and block and any("[in use]" in l for l in block):
        found = block
    return _kv(found or [])


def _num(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _run(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def query_iw(iw: str, ifaces: list[str]) -> LinkInfo | None:
    """Erstes verbundene Interface (bevorzugt in der Reihenfolge von ``ifaces``)."""
    first = None
    for iface in ifaces:
        info = parse_link(_run([iw, "dev", iface, "link"]), iface)
        if first is None:
            first = info
        if info.connected:
            info.station = parse_station_dump(_run([iw, "dev", iface, "station", "dump"]), info.bssid)
            info.survey = parse_survey(_run([iw, "dev", iface, "survey", "dump"]))
            return info
    return first


# ---------------------------------------------------------------------------------------------
# Aufbereitung
# ---------------------------------------------------------------------------------------------
def describe_bitrate(s: str) -> str:
    """'866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2' -> lesbarer Text."""
    if not s:
        return "–"
    m = re.match(r"([\d.]+)\s*MBit/s\s*(.*)", s)
    if not m:
        return s
    rate, rest = m.group(1), m.group(2)
    parts = [f"{float(rate):g} Mbit/s"]
    phy = None
    for tag, name in (("EHT", "802.11be"), ("HE", "802.11ax"), ("VHT", "802.11ac"), ("MCS", "802.11n")):
        if re.search(rf"\b{tag}-MCS\b" if tag != "MCS" else r"(^|\s)MCS\s", rest):
            phy = name
            break
    mcs = re.search(r"(?:EHT-|HE-|VHT-)?MCS (\d+)", rest)
    nss = re.search(r"NSS (\d+)", rest)
    width = re.search(r"(\d+)MHz", rest)
    details = []
    if phy:
        details.append(phy)
    if mcs:
        details.append(f"MCS {mcs.group(1)}")
    if nss:
        details.append(f"{nss.group(1)} Ströme")
    elif phy == "802.11n" and mcs:
        details.append(f"{int(mcs.group(1)) // 8 + 1} Ströme")
    if width:
        details.append(f"{width.group(1)} MHz")
    if "short GI" in rest:
        details.append("kurzes GI")
    gi = re.search(r"HE-GI (\d)", rest)
    if gi:
        details.append({"0": "GI 0,8 µs", "1": "GI 1,6 µs", "2": "GI 3,2 µs"}.get(gi.group(1), ""))
    if details:
        parts.append("(" + ", ".join(d for d in details if d) + ")")
    return " ".join(parts)


def _bytes(n: float | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _duration(sec: float | None) -> str:
    if sec is None:
        return "–"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


class ConnectionView:
    """Bildet aus LinkInfo-Abfragen die Anzeige; merkt sich den Vorwert für Raten."""

    def __init__(self):
        self.prev: LinkInfo | None = None      # vorletzte Abfrage (Basis für Raten)
        self.last: LinkInfo | None = None      # letzte Abfrage

    def sections(self, info: LinkInfo | None, aps: list[AccessPoint]) -> tuple[str, list[Section]]:
        if info is not self.last:            # neue Abfrage (nicht nur neu anzeigen)
            self.prev, self.last = self.last, info
        if info is None:
            return "Keine WLAN-Schnittstelle gefunden", []
        if not info.connected:
            return f"Nicht verbunden ({info.iface})", []
        prev = self.prev if self.prev and self.prev.connected and self.prev.bssid == info.bssid else None
        dt = info.ts - prev.ts if prev else 0.0
        st, ln, sv = info.station, info.link, info.survey
        ap = next((a for a in aps if a.bssid == info.bssid), None)

        def delta(key: str, src="station") -> float | None:
            if not prev or dt <= 0:
                return None
            a = _num(getattr(prev, src).get(key))
            b = _num(getattr(info, src).get(key))
            return None if a is None or b is None or b < a else b - a

        conn: list[Row] = [
            ("Status", "verbunden" + (" (autorisiert)" if st.get("authorized") == "yes" else "")),
            ("Interface", info.iface),
            ("SSID", info.ssid or "<versteckt>"),
            ("BSSID", info.bssid),
            ("Hersteller", (ap.vendor if ap else "") or oui.lookup(info.bssid) or "–"),
        ]
        if ap:
            conn += [("Band / Kanal", f"{ap.band} GHz, Kanal {ap.channel}, {ap.width} MHz"),
                     ("Wi-Fi-Generation", ap.generation or "–"),
                     ("Sicherheit", ap.security)]
        else:
            conn.append(("Frequenz", f"{info.freq:.0f} MHz"))
        conn += [("Verbunden seit", _duration(_num(st.get("connected time")))),
                 ("Beacon-Intervall", f"{ln['beacon int']} TU" if "beacon int" in ln else "–"),
                 ("DTIM-Periode", ln.get("dtim period", "–"))]

        sig = st.get("signal") or ln.get("signal", "")
        chains = re.search(r"\[([^\]]+)\]", sig)
        s_dbm = _num(sig)
        noise = _num(sv.get("noise"))
        radio: list[Row] = [("Signal", f"{s_dbm:.0f} dBm" if s_dbm is not None else "–")]
        if chains:
            radio.append(("Signal pro Antenne", chains.group(1).replace(",", " /") + " dBm"))
        avg = _num(st.get("signal avg"))
        if avg is not None:
            radio.append(("Signal (Mittel)", f"{avg:.0f} dBm"))
        if noise is not None:
            radio.append(("Rauschboden", f"{noise:.0f} dBm"))
            if s_dbm is not None:
                radio.append(("SNR", f"{s_dbm - noise:.0f} dB"))
        busy = delta("channel busy time", "survey")
        active = delta("channel active time", "survey")
        if busy is not None and active:
            radio.append(("Kanal belegt", f"{100 * busy / active:.0f} %  (eigene Messung)"))
        elif _num(sv.get("channel active time")):
            b, a = _num(sv.get("channel busy time")) or 0, _num(sv.get("channel active time"))
            radio.append(("Kanal belegt", f"{100 * b / a:.0f} %  (seit Start)"))

        rates: list[Row] = [
            ("Empfangsrate (RX)", describe_bitrate(st.get("rx bitrate") or ln.get("rx bitrate", ""))),
            ("Senderate (TX)", describe_bitrate(st.get("tx bitrate") or ln.get("tx bitrate", ""))),
        ]
        exp = _num(st.get("expected throughput"))
        if exp is not None:
            rates.append(("Erwarteter Durchsatz", f"{exp:g} Mbit/s"))
        rxb, txb = delta("rx bytes"), delta("tx bytes")
        if rxb is not None and txb is not None:
            rates.append(("Aktueller Verkehr", f"↓ {rxb * 8 / dt / 1e6:.2f}  ↑ {txb * 8 / dt / 1e6:.2f} Mbit/s"))

        stats: list[Row] = [
            ("Empfangen", f"{_bytes(_num(st.get('rx bytes')))}  ({st.get('rx packets', '–')} Pakete)"),
            ("Gesendet", f"{_bytes(_num(st.get('tx bytes')))}  ({st.get('tx packets', '–')} Pakete)"),
        ]
        txp, retr, fail = _num(st.get("tx packets")), _num(st.get("tx retries")), _num(st.get("tx failed"))
        if retr is not None:
            pct = f"  ({100 * retr / txp:.1f} %)" if txp else ""
            stats.append(("TX-Wiederholungen", f"{retr:.0f}{pct}"))
            dr, dp = delta("tx retries"), delta("tx packets")
            if dr is not None and dp:
                stats.append(("TX-Wiederholungen aktuell", f"{100 * dr / dp:.1f} %"))
        if fail is not None:
            pct = f"  ({100 * fail / txp:.2f} %)" if txp else ""
            stats.append(("TX fehlgeschlagen", f"{fail:.0f}{pct}"))
        if "rx drop misc" in st:
            stats.append(("RX verworfen", st["rx drop misc"]))
        if "inactive time" in st:
            stats.append(("Inaktiv seit", st["inactive time"]))

        roam: list[Row] = []
        if info.ssid:
            cands = sorted((a for a in aps if a.ssid == info.ssid and a.bssid != info.bssid),
                           key=lambda a: -a.signal)
            base = ap.signal if ap else s_dbm
            for a in cands[:8]:
                d = f"  ({a.signal - base:+.0f} dB)" if base is not None else ""
                roam.append((a.bssid, f"{a.band} GHz, Kanal {a.channel}, {a.signal:.0f} dBm{d}"))
            if not roam:
                roam.append(("–", "keine weiteren APs mit dieser SSID gefunden"))

        title = f"Verbunden mit {info.ssid or info.bssid}"
        out = [("Verbindung", conn), ("Funk", radio), ("Datenraten", rates), ("Statistik", stats)]
        if roam:
            out.append(("Roaming-Kandidaten (gleiche SSID)", roam))
        return title, out
