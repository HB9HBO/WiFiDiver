"""Auswertung mitgeschnittener 802.11-Frames: Radiotap-Kopf, MAC-Kopf, Informationselemente.

Die Daten kommen aus der Luft und sind nicht vertrauenswürdig: jeder Zugriff ist längengeprüft,
kaputte Frames werden verworfen (None), nie eine Ausnahme nach außen.
"""

from __future__ import annotations

import struct

from .channels import band_of_freq, channel_to_freq

# Radiotap-Felder des Standard-Namensraums: Bit -> (Ausrichtung, Größe)
_RT_FIELDS = {
    0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (1, 2), 5: (1, 1), 6: (1, 1), 7: (2, 2),
    8: (2, 2), 9: (2, 2), 10: (1, 1), 11: (1, 1), 12: (1, 1), 13: (1, 1), 14: (2, 2), 15: (2, 2),
    16: (1, 1), 17: (1, 1), 18: (4, 8), 19: (1, 3), 20: (4, 8), 21: (2, 12), 22: (8, 12),
    23: (2, 12), 24: (2, 12), 25: (2, 6), 26: (1, 1), 27: (2, 4),
}
_RT_WANTED = (1, 2, 5, 19, 20, 21, 23)
_RT_CACHE: dict[tuple, dict[int, int] | None] = {}

# Datenraten pro Spatial Stream bei 20 MHz und langem Guard Interval (Mbit/s)
_HT20 = (6.5, 13, 19.5, 26, 39, 52, 58.5, 65)
_VHT20 = (6.5, 13, 19.5, 26, 39, 52, 58.5, 65, 78, 86.7, 97.5, 108.3)
_HE20 = (8.6, 17.2, 25.8, 34.4, 51.6, 68.8, 77.4, 86, 103.2, 114.7, 129, 143.4, 154.9, 172.1)
_BW_FACTOR = {20: 1.0, 40: 2.08, 80: 4.5, 160: 9.0, 320: 18.0}


def _rt_layout(present: tuple[int, ...]) -> dict[int, int] | None:
    """Offsets der gewünschten Felder (nur erstes Present-Wort; Daten beginnen nach allen Wörtern)."""
    lay = _RT_CACHE.get(present)
    if lay is not None or present in _RT_CACHE:
        return lay
    off = 8 + 4 * (len(present) - 1)
    first = present[0]
    lay = {}
    for bit in range(29):
        if not first & (1 << bit):
            continue
        fld = _RT_FIELDS.get(bit)
        if fld is None:        # unbekanntes Feld: Rest nicht auswertbar
            break
        align, size = fld
        off = (off + align - 1) & ~(align - 1)
        if bit in _RT_WANTED:
            lay[bit] = off
        off += size
    _RT_CACHE[present] = lay
    return lay


class Radiotap:
    __slots__ = ("length", "flags", "signal", "rate", "ampdu", "phy")

    def __init__(self):
        self.length = 0
        self.flags = 0
        self.signal: int | None = None
        self.rate: float | None = None     # Mbit/s
        self.ampdu: int | None = None      # Referenznummer des A-MPDU
        self.phy = "ofdm"                  # dsss / ofdm / ht / vht / he


def parse_radiotap(b: bytes) -> Radiotap | None:
    n = len(b)
    if n < 8 or b[0] != 0:
        return None
    rtlen = b[2] | (b[3] << 8)
    if rtlen < 8 or rtlen > n:
        return None
    words = []
    p = 4
    while True:
        if p + 4 > rtlen:
            return None
        w = int.from_bytes(b[p:p + 4], "little")
        words.append(w)
        p += 4
        if not w & 0x80000000:
            break
        if len(words) > 8:
            return None
    lay = _rt_layout(tuple(words))
    rt = Radiotap()
    rt.length = rtlen
    if not lay:
        return rt
    o = lay.get(1)
    if o is not None and o < rtlen:
        rt.flags = b[o]
    o = lay.get(5)
    if o is not None and o < rtlen:
        s = b[o]
        rt.signal = s - 256 if s > 127 else s
    o = lay.get(2)
    if o is not None and o < rtlen and b[o]:
        rt.rate = b[o] * 0.5
        rt.phy = "dsss" if rt.rate in (1.0, 2.0, 5.5, 11.0) else "ofdm"
    o = lay.get(20)
    if o is not None and o + 4 <= rtlen:
        rt.ampdu = int.from_bytes(b[o:o + 4], "little")
    o = lay.get(19)
    if o is not None and o + 3 <= rtlen:
        known, fl, mcs = b[o], b[o + 1], b[o + 2]
        if known & 0x02:
            bw = 40 if (fl & 0x03) == 1 else 20
            sgi = bool(known & 0x04 and fl & 0x04)
            nss = mcs // 8 + 1
            r = _HT20[mcs % 8] * nss * (2.08 if bw == 40 else 1.0)
            rt.rate = r * (10 / 9 if sgi else 1.0)
            rt.phy = "ht"
    o = lay.get(21)
    if o is not None and o + 12 <= rtlen:
        flags, bwc, mcs_nss = b[o + 2], b[o + 3], b[o + 4]
        mcs, nss = mcs_nss >> 4, mcs_nss & 0x0F
        if nss and mcs < len(_VHT20):
            bw = 160 if bwc >= 11 else 80 if bwc >= 4 else 40 if bwc >= 1 else 20
            r = _VHT20[mcs] * nss * _BW_FACTOR[bw]
            rt.rate = r * (10 / 9 if flags & 0x04 else 1.0)
            rt.phy = "vht"
    o = lay.get(23)
    if o is not None and o + 12 <= rtlen:
        d1, d3, d5, d6 = struct.unpack_from("<H2xHxxHH", b, o)
        if d1 & 0x0020:                       # MCS bekannt
            mcs = (d3 >> 8) & 0x0F
            nss = (d6 & 0x0F) or 1
            bwc = d5 & 0x0F
            bw = {0: 20, 1: 40, 2: 80, 3: 160}.get(bwc, 20)
            gi = (d5 >> 4) & 0x03              # 0: 0.8 µs, 1: 1.6 µs, 2: 3.2 µs
            if mcs < len(_HE20):
                r = _HE20[mcs] * nss * _BW_FACTOR[bw]
                rt.rate = r * (1.0, 0.94, 0.85)[min(gi, 2)]
                rt.phy = "he"
    return rt


_PREAMBLE_US = {"dsss": 192.0, "ofdm": 20.0, "ht": 36.0, "vht": 40.0, "he": 48.0}


def airtime_us(rt: Radiotap, frame_len: int, new_ppdu: bool = True) -> float:
    """Geschätzte Sendedauer eines Frames (µs). Ohne Rate: 24 Mbit/s angenommen."""
    rate = rt.rate or 24.0
    t = frame_len * 8.0 / rate
    if new_ppdu:
        t += _PREAMBLE_US.get(rt.phy, 20.0)
    return t


def mac(b: bytes, off: int) -> str:
    return b[off:off + 6].hex(":")


def is_unicast(m: str) -> bool:
    return not int(m[:2], 16) & 1


def is_random_mac(m: str) -> bool:
    """Lokal verwaltete Adresse (Bit 1 im ersten Byte) – meist eine zufällige Client-Adresse."""
    return bool(int(m[:2], 16) & 2)


# ----------------------------------------------------------------------------- Informationselemente
_AKM = {1: "802.1X", 2: "PSK", 3: "FT/802.1X", 4: "FT/PSK", 5: "802.1X/SHA-256", 6: "PSK/SHA-256",
        8: "SAE", 9: "FT/SAE", 11: "802.1X/SUITE-B", 12: "802.1X/SUITE-B-192", 18: "OWE",
        24: "SAE-EXT-KEY", 25: "FT/SAE-EXT-KEY"}


def iter_ies(b: bytes, off: int):
    n = len(b)
    while off + 2 <= n:
        eid, ln = b[off], b[off + 1]
        if off + 2 + ln > n:
            return
        yield eid, b[off + 2:off + 2 + ln]
        off += 2 + ln


def _security(akms: list[str] | None, wpa: bool, privacy: bool) -> str:
    parts: list[str] = []
    if akms is not None:
        psk = any(a in ("PSK", "FT/PSK", "PSK/SHA-256") for a in akms)
        sae = any("SAE" in a for a in akms)
        eap = any("802.1X" in a for a in akms)
        if sae and psk:
            parts.append("WPA2/WPA3-Personal")
        elif sae:
            parts.append("WPA3-Personal")
        elif psk:
            parts.append("WPA2-Personal")
        if eap:
            parts.append("WPA3-Enterprise" if any("SUITE-B" in a or "SHA-256" in a for a in akms)
                         else "WPA2-Enterprise")
        if "OWE" in akms:
            parts.append("OWE")
        if not parts:
            parts.append("WPA2")
        if any(a.startswith("FT/") for a in akms):
            parts.append("802.11r")
    if wpa:
        parts.insert(0, "WPA")
    if not parts:
        return "WEP" if privacy else "Offen"
    return " + ".join(parts)


def parse_beacon_body(body: bytes, tuned_freq: float) -> dict | None:
    """Beacon/Probe-Response ab dem Zeitstempel: SSID, Kanal, Breite, Sicherheit, Generation …"""
    if len(body) < 12:
        return None
    interval = body[8] | (body[9] << 8)
    cap = body[10] | (body[11] << 8)
    band = band_of_freq(tuned_freq)
    info: dict = {"beacon_interval": interval, "ssid": None, "primary": None, "width": 20,
                  "center_ch": None, "ht_offset": None, "akms": None, "wpa": False,
                  "privacy": bool(cap & 0x0010), "gen": "Legacy", "stations": None,
                  "utilization": None, "country": "", "pmf": ""}
    gens = set()
    width_he6 = None
    eht = None
    for eid, d in iter_ies(body, 12):
        if eid == 0:
            if info["ssid"] is None:
                info["ssid"] = d.decode("utf-8", "replace") if any(d) else ""
        elif eid == 3 and d:
            info["primary"] = d[0]
        elif eid == 7 and len(d) >= 2:
            info["country"] = d[:2].decode("ascii", "replace")
        elif eid == 11 and len(d) >= 3:
            info["stations"] = d[0] | (d[1] << 8)
            info["utilization"] = d[2] * 100.0 / 255.0
        elif eid == 45:
            gens.add(4)
        elif eid == 61 and len(d) >= 2:
            info["primary"] = info["primary"] or d[0]
            off = d[1] & 0x03
            if d[1] & 0x04 and off in (1, 3):
                info["width"] = 40
                info["ht_offset"] = "above" if off == 1 else "below"
        elif eid == 191:
            gens.add(5)
        elif eid == 192 and len(d) >= 3:
            w, c0, c1 = d[0], d[1], d[2]
            if w == 1 and c0:
                if c1 == 0:
                    info["width"], info["center_ch"] = 80, c0
                elif abs(c1 - c0) == 8:
                    info["width"], info["center_ch"] = 160, c1
                else:
                    info["width"], info["center_ch"] = 160, c0
                    info["80+80"] = (c0, c1)
            elif w == 2 and c0:
                info["width"], info["center_ch"] = 160, c0
        elif eid == 48 and len(d) >= 8:
            try:
                p = 2 + 4
                npw = d[p] | (d[p + 1] << 8)
                p += 2 + 4 * npw
                nak = d[p] | (d[p + 1] << 8)
                p += 2
                akms = []
                for i in range(nak):
                    s = d[p + 4 * i:p + 4 * i + 4]
                    if len(s) == 4:
                        akms.append(_AKM.get(s[3], f"AKM{s[3]}") if s[:3] == b"\x00\x0f\xac" else "Vendor")
                info["akms"] = akms
                p += 4 * nak
                if p + 2 <= len(d):          # RSN-Fähigkeiten: Bit 6 MFPR, Bit 7 MFPC
                    caps = d[p] | (d[p + 1] << 8)
                    info["pmf"] = "required" if caps & 0x40 else ("capable" if caps & 0x80 else "")
            except IndexError:
                info["akms"] = []
        elif eid == 221 and len(d) >= 4 and d[:4] == b"\x00\x50\xf2\x01":
            info["wpa"] = True
        elif eid == 255 and d:
            ext = d[0]
            if ext == 35:
                gens.add(6)
            elif ext == 108:
                gens.add(7)
            elif ext == 36 and len(d) >= 7:
                params = d[1] | (d[2] << 8) | (d[3] << 16)
                p = 7
                if params & (1 << 14):
                    p += 3
                if params & (1 << 15):
                    p += 1
                if params & (1 << 17) and p + 5 <= len(d):
                    prim, ctrl, c0, c1 = d[p], d[p + 1], d[p + 2], d[p + 3]
                    w = (20, 40, 80, 160)[ctrl & 0x03]
                    width_he6 = (prim, w, c1 if w == 160 and c1 else c0)
            elif ext == 106 and len(d) >= 6:
                if d[1] & 0x01 and len(d) >= 9:
                    w = {0: 20, 1: 40, 2: 80, 3: 160, 4: 320}.get(d[6] & 0x07)
                    c0, c1 = d[7], d[8]
                    if w:
                        eht = (w, c1 if w >= 160 and c1 else c0)
    if gens:
        info["gen"] = f"Wi-Fi {max(gens)}"
    if width_he6 is not None:
        prim, w, cch = width_he6
        info["primary"], info["width"], info["center_ch"] = prim, w, cch
    if eht is not None and eht[0] > info["width"]:
        info["width"], info["center_ch"] = eht
    # Frequenzen: Kanalnummern gehören zum Band der eingestellten Frequenz
    prim = info["primary"]
    info["freq"] = float(tuned_freq)
    if prim and band:
        try:
            info["freq"] = channel_to_freq(prim, band)
        except ValueError:
            pass
    info["center"] = None
    if info["center_ch"] and band:
        try:
            info["center"] = channel_to_freq(info["center_ch"], band)
        except ValueError:
            pass
    info["security"] = _security(info["akms"], info["wpa"], info["privacy"])
    return info
