"""Parser für die Ausgabe von `iw dev <if> scan dump`."""

from __future__ import annotations

import re

from .channels import band_of_freq, channel_to_freq
from .model import AccessPoint

_BSS_RE = re.compile(r"^BSS ([0-9a-fA-F:]{17})")
_SEG_RE = re.compile(r"center freq(?:uency)? segment\s*([012])\s*:\s*(\d+)", re.I)
_WIDTH_MHZ_RE = re.compile(r"channel width\s*:\s*(?:\d+\s*\()?\s*(\d+)\s*MHz", re.I)
_WIDTH_CODE_RE = re.compile(r"channel width\s*:\s*(\d+)\b", re.I)


def _unescape_ssid(raw: str) -> str:
    """iw gibt nicht druckbare Bytes als \\xNN aus."""
    if "\\x" not in raw:
        return raw
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw.startswith("\\x", i) and i + 4 <= len(raw):
            try:
                out.append(int(raw[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        out.extend(raw[i].encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _split_blocks(text: str) -> list[tuple[str, list[str], bool]]:
    blocks = []
    cur: list[str] | None = None
    bssid = ""
    assoc = False
    for line in text.splitlines():
        m = _BSS_RE.match(line)
        if m:
            if cur is not None:
                blocks.append((bssid, cur, assoc))
            bssid = m.group(1).lower()
            assoc = "associated" in line
            cur = []
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append((bssid, cur, assoc))
    return blocks


def _sections(lines: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Top-Level-Felder (eine Tabulator-Einrückung) und deren Unterzeilen."""
    fields: dict[str, str] = {}
    subs: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip("\t"))
        body = line.strip()
        if depth <= 1 and not body.startswith("*"):
            key, _, val = body.partition(":")
            key = key.strip()
            current = key
            val = val.strip()
            # z.B. "RSN:\t * Version: 1" -> erste Unterzeile in derselben Zeile
            if val.startswith("*"):
                subs.setdefault(key, []).append(val.lstrip("* ").strip())
                val = ""
            if key not in fields:
                fields[key] = val
            subs.setdefault(key, [])
        elif current is not None:
            subs[current].append(body.lstrip("* ").strip())
    return fields, subs


def _security(fields: dict[str, str], subs: dict[str, list[str]]) -> str:
    parts: list[str] = []
    if "RSN" in fields:
        auth = ""
        for s in subs.get("RSN", []):
            if s.lower().startswith("authentication suites"):
                auth = s.partition(":")[2]
        tokens = auth.split()
        has_psk = any(t in ("PSK", "FT/PSK", "PSK/SHA-256") for t in tokens)
        has_sae = any("SAE" in t for t in tokens)
        has_eap = any("802.1X" in t for t in tokens)
        has_owe = "OWE" in tokens
        if has_sae and has_psk:
            parts.append("WPA2/WPA3-Personal")
        elif has_sae:
            parts.append("WPA3-Personal")
        elif has_psk:
            parts.append("WPA2-Personal")
        if has_eap:
            parts.append("WPA3-Enterprise" if any("SUITE-B" in t or "SHA256" in t for t in tokens)
                         else "WPA2-Enterprise")
        if has_owe:
            parts.append("OWE")
        if not parts:
            parts.append("WPA2")
        if any(t.startswith("FT/") for t in tokens):
            parts.append("802.11r")
    if "WPA" in fields:
        parts.insert(0, "WPA")
    if not parts:
        cap = fields.get("capability", "")
        return "WEP" if "Privacy" in cap else "Offen"
    return " + ".join(parts)


def _generation(fields: dict[str, str]) -> str:
    keys = {k.lower() for k in fields}
    if any(k.startswith("eht") for k in keys):
        return "Wi-Fi 7"
    if any(k.startswith("he ") or k == "he capabilities" or k.startswith("he operation") for k in keys):
        return "Wi-Fi 6"
    if any(k.startswith("vht") for k in keys):
        return "Wi-Fi 5"
    if any(k.startswith("ht ") for k in keys):
        return "Wi-Fi 4"
    return "Legacy"


def _width_from_text(lines: list[str]) -> tuple[int | None, dict[int, int]]:
    width = None
    segs: dict[int, int] = {}
    for s in lines:
        m = _SEG_RE.search(s)
        if m:
            segs[int(m.group(1))] = int(m.group(2))
            continue
        m = _WIDTH_MHZ_RE.search(s)
        if m:
            width = int(m.group(1))
    return width, segs


def parse_ap(bssid: str, lines: list[str], associated: bool = False) -> AccessPoint | None:
    fields, subs = _sections(lines)
    try:
        freq = float(fields.get("freq", "0").split()[0])
    except (ValueError, IndexError):
        return None
    band = band_of_freq(freq)
    if band is None:
        return None

    ap = AccessPoint(bssid=bssid, freq=freq, connected=associated)
    ap.ssid = _unescape_ssid(fields.get("SSID", ""))
    sig = fields.get("signal", "")
    try:
        ap.signal = float(sig.split()[0])
    except (ValueError, IndexError):
        pass
    for line in lines:   # neuere iw-Versionen haben zwei "last seen"-Zeilen
        m = re.search(r"last seen:\s*(\d+)\s*ms ago", line)
        if m:
            ap.last_seen_ms = int(m.group(1))
            break

    ap.security = _security(fields, subs)
    ap.generation = _generation(fields)

    for s in subs.get("BSS Load", []):
        if s.startswith("station count"):
            try:
                ap.stations = int(s.partition(":")[2])
            except ValueError:
                pass
        elif s.startswith("channel utilisation") or s.startswith("channel utilization"):
            m = re.search(r"(\d+)\s*/\s*255", s)
            if m:
                ap.utilization = int(m.group(1)) * 100.0 / 255.0

    # --- Kanalbreite ---------------------------------------------------
    width = 20
    center_ch = None
    for s in subs.get("HT operation", []):
        if s.startswith("secondary channel offset"):
            off = s.partition(":")[2].strip()
            if off in ("above", "below"):
                ap.ht_offset = off
                width = 40

    vht = subs.get("VHT operation", [])
    if vht:
        code = None
        segs: dict[int, int] = {}
        for s in vht:
            m = _WIDTH_CODE_RE.search(s)
            if m and code is None:
                code = int(m.group(1))
            m = _SEG_RE.search(s)
            if m:
                segs[int(m.group(1))] = int(m.group(2))
        s1, s2 = segs.get(1, 0), segs.get(2, 0)
        if code == 1:
            if s2 and abs(s2 - s1) == 8:
                width, center_ch = 160, s2
            elif s2:
                width, center_ch = 160, s1   # 80+80 als 160 dargestellt
                ap.extra["80+80"] = (s1, s2)
            else:
                width, center_ch = 80, s1
        elif code in (2, 3):
            width, center_ch = 160, s1

    # HE (6 GHz Operation Information) und EHT Operation
    for key in list(subs):
        lk = key.lower()
        if lk.startswith("he operation") or lk.startswith("eht operation"):
            w, segs = _width_from_text(subs[key])
            if band == "6" and segs:
                s0 = segs.get(0, 0)
                s1 = segs.get(1, 0)
                if s1 and abs(s1 - s0) == 16:
                    width, center_ch = 320, s1
                elif s1 and abs(s1 - s0) == 8:
                    width, center_ch = 160, s1
                else:
                    center_ch = s0 or center_ch
                    if w:
                        width = w
            if lk.startswith("eht operation") and w and w > width:
                width = w
                if segs:
                    center_ch = segs.get(1) or segs.get(0) or center_ch
            if lk.startswith("eht operation") and segs and 2 in segs and w == 320:
                center_ch = segs[2]

    ap.width = width
    if center_ch:
        try:
            ap.center_freq = channel_to_freq(center_ch, band)
        except ValueError:
            pass
    return ap.finalize()


def parse_scan_dump(text: str) -> list[AccessPoint]:
    aps = []
    for bssid, lines, assoc in _split_blocks(text):
        ap = parse_ap(bssid, lines, assoc)
        if ap is not None:
            aps.append(ap)
    return aps
