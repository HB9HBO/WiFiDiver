"""Aufbereitete Detailangaben zu einem Access Point (Details-Tab).

Grundlage sind die Informationselemente aus `iw scan dump` (``ap.extra["ies"]``). Für Quellen
ohne Rohdaten (nmcli, Demo) gibt es nur die allgemeinen Angaben.
"""

from __future__ import annotations

import re

from . import oui
from .channels import DFS_CHANNELS_5G
from .iw_parser import _sections
from .model import AccessPoint

Row = tuple[str, str]
Section = tuple[str, list[Row]]

# Maximale Datenrate pro räumlichem Strom (Mbit/s) bei höchster MCS und kurzem Guard-Intervall
_RATE_PER_STREAM = {
    "Wi-Fi 4": {20: 72.2, 40: 150.0},                                  # HT MCS 7, 400 ns GI
    "Wi-Fi 5": {20: 86.7, 40: 200.0, 80: 433.3, 160: 866.7},           # VHT MCS 9 (20 MHz: MCS 8)
    "Wi-Fi 6": {20: 143.4, 40: 286.8, 80: 600.5, 160: 1201.0},         # HE MCS 11, 0,8 µs GI
    "Wi-Fi 7": {20: 172.1, 40: 344.1, 80: 720.6, 160: 1441.2, 320: 2882.4},   # EHT MCS 13
}
_PHY = {"Wi-Fi 7": "802.11be (EHT)", "Wi-Fi 6": "802.11ax (HE)", "Wi-Fi 5": "802.11ac (VHT)",
        "Wi-Fi 4": "802.11n (HT)"}

# bekannte Hersteller-IEs (OUI -> Bedeutung), ergänzt um die IEEE-OUI-Datenbank
_KNOWN_OUI = {
    "00:50:f2": "Microsoft (WMM/WPA/WPS)",
    "50:6f:9a": "Wi-Fi Alliance (Passpoint/MBO/OWE)",
    "00:10:18": "Broadcom",
    "00:03:7f": "Qualcomm Atheros",
    "00:0c:43": "MediaTek (Ralink)",
    "00:e0:4c": "Realtek",
    "00:17:f2": "Apple",
    "00:40:96": "Cisco (CCX)",
    "00:0b:86": "Aruba",
    "00:13:92": "Ruckus",
    "8c:fd:f0": "Qualcomm",
    "00:90:4c": "Epigram (Broadcom HT)",
    "00:26:86": "Quantenna",
    "f4:f5:e8": "Google",
    "00:a0:40": "Apple",
}


def _streams(subs: list[str], pattern: str) -> int:
    """Höchste Anzahl räumlicher Ströme aus Zeilen wie '2 streams: MCS 0-9'."""
    best = 0
    for s in subs:
        m = re.match(pattern, s)
        if m and "not supported" not in s:
            best = max(best, int(m.group(1)))
    return best


def _first(subs: list[str], prefix: str) -> str:
    pl = prefix.lower()
    for s in subs:
        if s.lower().startswith(pl):
            return s.partition(":")[2].strip()
    return ""


def _rates(text: str) -> tuple[str, str]:
    """'6.0* 9.0 12.0* …' -> (alle Raten, Basisraten)."""
    toks = text.split()
    allr = [t.rstrip("*") for t in toks]
    basic = [t.rstrip("*") for t in toks if t.endswith("*")]
    return " ".join(allr), " ".join(basic)


def _fmt_rate(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v
    return f"{f:g}"


def spatial_streams(fields: dict, subs: dict) -> int:
    n = 0
    for key, lines in subs.items():
        k = key.lower()
        if k.startswith("he capabilities") or k.startswith("vht capabilities") or k.startswith("eht cap"):
            n = max(n, _streams(lines, r"(\d+) streams?:\s*MCS"))
    for s in subs.get("HT capabilities", []):
        m = re.search(r"MCS rate indexes supported:\s*0-(\d+)", s)
        if m:
            n = max(n, (int(m.group(1)) + 1) // 8)
    return n


def max_phy_rate(gen: str, width: int, streams: int) -> float | None:
    table = _RATE_PER_STREAM.get(gen)
    if not table or streams <= 0:
        return None
    w = max((x for x in table if x <= width), default=None)
    return None if w is None else table[w] * streams


def _vendor_ies(lines: list[str]) -> str:
    seen: list[str] = []
    for line in lines:
        m = re.search(r"Vendor specific:\s*OUI\s*([0-9a-fA-F:]{8})", line)
        if m:
            o = m.group(1).lower()
            if o not in seen:
                seen.append(o)
        elif line.strip().startswith(("WMM:", "WPA:", "WPS:")) and "00:50:f2" not in seen:
            seen.append("00:50:f2")
    out = []
    for o in seen:
        name = _KNOWN_OUI.get(o) or oui.lookup(o + ":00:00:00") or "?"
        out.append(f"{o} ({name})")
    return ", ".join(out)


def _pmf(rsn_caps: str) -> str:
    if "MFP-required" in rsn_caps:
        return "erforderlich"
    if "MFP-capable" in rsn_caps:
        return "optional (unterstützt)"
    return "nein"


def _yes(v: bool) -> str:
    return "ja" if v else "nein"


def ap_details(ap: AccessPoint) -> list[Section]:
    """Abschnitte (Titel, [(Bezeichnung, Wert), …]) für die Anzeige."""
    lines: list[str] = ap.extra.get("ies") or []
    fields, subs = _sections(lines) if lines else ({}, {})

    ch = ap.channel
    chan = f"{ch}" + ("  (DFS)" if ap.band == "5" and ch in DFS_CHANNELS_5G else "")
    general: list[Row] = [
        ("SSID", ap.display_ssid),
        ("BSSID", ap.bssid),
        ("Hersteller", ap.vendor or "–"),
        ("Wi-Fi-Generation", ap.generation or "–"),
        ("Standard", _PHY.get(ap.generation, "802.11a/b/g" if ap.generation else "–")),
        ("Band", f"{ap.band} GHz" if ap.band else "–"),
        ("Kanal", chan),
        ("Primärfrequenz", f"{ap.freq:.0f} MHz"),
        ("Mittenfrequenz", f"{ap.center_freq:.0f} MHz"),
        ("Kanalbreite", f"{ap.width} MHz" + ("  (80+80)" if "80+80" in ap.extra else "")),
        ("Signal", f"{ap.signal:.0f} dBm"),
        ("Zuletzt gesehen", "–" if ap.last_seen_ms is None else
         (f"vor {ap.last_seen_ms} ms" if ap.last_seen_ms < 1000 else f"vor {ap.last_seen_ms / 1000:.1f} s")),
        ("Verbunden", "ja (dieses Gerät)" if ap.connected else "nein"),
    ]
    out: list[Section] = [("Allgemein", general)]
    if not lines:
        general.append(("Hinweis", "Weitere Details (Informationselemente) nur mit der Quelle „iw“"))
        out.append(("Sicherheit", [("Sicherheit", ap.security)]))
        return out

    # --- Allgemein, Fortsetzung -------------------------------------------------------------
    frame = next((l.strip() for l in lines if "Information elements from" in l), "")
    if frame:
        general.append(("Quelle der Daten", "Probe Response" if "Probe" in frame else "Beacon"))
    country = fields.get("Country", "")
    if country:
        code, _, env = country.partition("\t")
        env = env.replace("Environment:", "").strip()
        general.append(("Land (802.11d)", f"{code.strip()}  {env}".strip()))
    bi = fields.get("beacon interval", "")
    m = re.match(r"(\d+)", bi)
    if m:
        tu = int(m.group(1))
        general.append(("Beacon-Intervall", f"{tu} TU ({tu * 1.024:.1f} ms)"))
    tim = fields.get("TIM", "")
    m = re.search(r"DTIM Period (\d+)", tim)
    if m:
        general.append(("DTIM-Periode", m.group(1)))
    cap = fields.get("capability", "")
    if cap:
        general.append(("Capabilities", re.sub(r"\s*\(0x[0-9a-f]+\)", "", cap)))

    # --- Funk / PHY --------------------------------------------------------------------------
    phy: list[Row] = []
    streams = spatial_streams(fields, subs)
    if streams:
        phy.append(("Räumliche Ströme (MIMO)", str(streams)))
    rate = max_phy_rate(ap.generation, ap.width, streams)
    if rate:
        phy.append(("Max. PHY-Rate (geschätzt)", f"{rate:,.0f} Mbit/s".replace(",", "'")))
    sup = fields.get("Supported rates", "")
    ext = fields.get("Extended supported rates", "")
    if sup or ext:
        allr, basic = _rates(f"{sup} {ext}")
        phy.append(("Legacy-Raten", " ".join(_fmt_rate(r) for r in allr.split()) + " Mbit/s"))
        if basic:
            phy.append(("Basisraten", " ".join(_fmt_rate(r) for r in basic.split()) + " Mbit/s"))
    feats = []
    ht = subs.get("HT capabilities", [])
    if any("SGI" in s for s in ht):
        feats.append("HT Short GI")
    if any("LDPC" in s for s in ht):
        feats.append("LDPC")
    for key, ls in subs.items():
        if key.lower().startswith("vht capabilities"):
            if any("SU Beamformer" in s for s in ls):
                feats.append("VHT SU-Beamformer")
            if any("MU Beamformer" in s for s in ls):
                feats.append("VHT MU-MIMO")
        if key.lower().startswith("he capabilities"):
            if any("SU Beamformer" in s for s in ls):
                feats.append("HE SU-Beamformer")
            if any("MU Beamformer" in s for s in ls):
                feats.append("HE MU-MIMO")
            if any("TWT Responder" in s for s in ls):
                feats.append("TWT")
            if any("1024-QAM" in s for s in ls):
                feats.append("1024-QAM")
            if any("Punctured" in s or "Preamble Puncturing" in s for s in ls):
                feats.append("Preamble Puncturing")
    if feats:
        phy.append(("PHY-Merkmale", ", ".join(dict.fromkeys(feats))))
    color = None
    for key, ls in subs.items():
        if key.lower().startswith("he operation"):
            for s in ls:
                m = re.search(r"BSS Color\D*(\d+)", s, re.I)
                if m:
                    color = m.group(1)
    if color:
        phy.append(("BSS-Color (HE)", color))
    if "80+80" in ap.extra:
        a, b = ap.extra["80+80"]
        phy.append(("80+80-Segmente", f"Kanal {a} und {b}"))
    if phy:
        out.append(("Funk / PHY", phy))

    # --- Sendeleistung ----------------------------------------------------------------------
    pw: list[Row] = []
    tpc = fields.get("TPC report", "")
    m = re.search(r"(-?\d+)\s*dBm", tpc)
    if m:
        pw.append(("Sendeleistung (TPC)", f"{m.group(1)} dBm"))
    pc = fields.get("Power constraint", "")
    if pc:
        pw.append(("Leistungsreduktion", pc))
    lim = [s for s in subs.get("Country", []) if "dBm" in s]
    if lim:
        pw.append(("Grenzwerte (802.11d)", "; ".join(lim)))
    for key, ls in subs.items():
        if "power" in key.lower() and "6" in key and ls:      # 6-GHz-AP-Leistungstyp
            pw.append((key, "; ".join(ls)))
    if pw:
        out.append(("Sendeleistung", pw))

    # --- Sicherheit -------------------------------------------------------------------------
    sec: list[Row] = [("Profil", ap.security)]
    rsn = subs.get("RSN", [])
    if rsn:
        sec.append(("RSN-Version", _first(rsn, "Version") or "–"))
        sec.append(("Gruppen-Cipher", _first(rsn, "Group cipher") or "–"))
        sec.append(("Paarweise Cipher", _first(rsn, "Pairwise ciphers") or "–"))
        sec.append(("AKM (Authentifizierung)", _first(rsn, "Authentication suites") or "–"))
        caps = _first(rsn, "Capabilities")
        sec.append(("PMF / 802.11w", _pmf(caps)))
        gm = _first(rsn, "Group mgmt cipher")
        if gm:
            sec.append(("Mgmt-Cipher (BIP)", gm))
        if caps:
            sec.append(("RSN-Capabilities", re.sub(r"\s*\(0x[0-9a-f]+\)", "", caps)))
    wpa = subs.get("WPA", [])
    if wpa:
        sec.append(("WPA (alt)", f"{_first(wpa, 'Pairwise ciphers')} / {_first(wpa, 'Authentication suites')}"))
    for key, ls in subs.items():
        if key.upper().startswith("RSNX") and ls:
            sec.append(("RSNX (SAE-Erweiterungen)", ", ".join(ls)))
    wps = subs.get("WPS", [])
    if wps:
        st = _first(wps, "Wi-Fi Protected Setup State")
        sec.append(("WPS", "aktiv" + (f", {st}" if st else "")))
        for lbl, key in (("WPS: Hersteller", "Manufacturer"), ("WPS: Modell", "Model"),
                         ("WPS: Gerätename", "Device name")):
            v = _first(wps, key)
            if v:
                sec.append((lbl, v))
    out.append(("Sicherheit", sec))

    # --- Roaming & Auslastung ---------------------------------------------------------------
    rm = subs.get("RM enabled capabilities", [])
    ext_caps = subs.get("Extended capabilities", [])
    md = fields.get("Mobility Domain", "") or fields.get("MD", "")
    roam: list[Row] = [
        ("802.11k (Neighbor Report)", _yes(any("Neighbor Report" in s for s in rm))),
        ("802.11v (BSS Transition)", _yes(any("BSS Transition" in s for s in ext_caps))),
        ("802.11r (Fast Transition)", (md.strip() or "ja") if (md or "802.11r" in ap.security) else "nein"),
    ]
    load = subs.get("BSS Load", [])
    if load:
        roam.append(("Clients (BSS Load)", _first(load, "station count") or "–"))
        if ap.utilization is not None:
            roam.append(("Kanalauslastung", f"{ap.utilization:.0f} %  ({_first(load, 'channel utili')})"))
        aac = _first(load, "available admission capacity")
        if aac:
            roam.append(("Freie Zugangskapazität", aac))
    out.append(("Roaming & Auslastung", roam))

    vend = _vendor_ies(lines)
    if vend:
        out.append(("Hersteller-IEs", [("OUIs", vend)]))
    return out


def raw_text(ap: AccessPoint) -> str:
    lines = ap.extra.get("ies") or []
    return "\n".join(l.replace("\t", "  ") for l in lines)
