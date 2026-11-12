"""Kanal-/Frequenz-Mathematik für 2.4, 5 und 6 GHz WLAN."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    key: str          # "2.4", "5", "6"
    name: str         # Anzeigename
    f_min: float      # MHz, Anzeigebereich
    f_max: float      # MHz


BANDS: dict[str, Band] = {
    "2.4": Band("2.4", "2.4 GHz", 2400.0, 2495.0),
    "5": Band("5", "5 GHz", 5150.0, 5895.0),
    "6": Band("6", "6 GHz", 5925.0, 7125.0),
}

# 5-GHz-Kanäle, die DFS benötigen (ETSI/FCC, UNII-2/2e)
DFS_CHANNELS_5G = set(range(52, 65, 4)) | set(range(100, 145, 4))


def band_of_freq(freq_mhz: float) -> str | None:
    if 2400 <= freq_mhz <= 2500:
        return "2.4"
    if 5150 <= freq_mhz <= 5925:
        return "5"
    if 5925 < freq_mhz <= 7125:
        return "6"
    return None


def freq_to_channel(freq_mhz: float) -> int:
    f = int(round(freq_mhz))
    if f == 2484:
        return 14
    if 2407 < f < 2484:
        return (f - 2407) // 5
    if 5935 == f:
        return 2
    if 5950 < f <= 7125:
        return (f - 5950) // 5
    if 5000 < f <= 5925:
        return (f - 5000) // 5
    return 0


def channel_to_freq(channel: int, band: str) -> float:
    if band == "2.4":
        return 2484.0 if channel == 14 else 2407.0 + 5 * channel
    if band == "5":
        return 5000.0 + 5 * channel
    if band == "6":
        return 5935.0 if channel == 2 else 5950.0 + 5 * channel
    raise ValueError(f"unbekanntes Band {band}")


def standard_center_channel(primary: int, width: int, band: str,
                            ht_offset: str | None = None) -> int:
    """Mittenkanal nach Standard-Kanalraster schätzen (wenn nicht im Beacon)."""
    if width <= 20:
        return primary
    if band == "2.4":
        if ht_offset == "below" or (ht_offset is None and primary > 7):
            return primary - 2
        return primary + 2
    n = width // 20  # Anzahl 20-MHz-Teilkanäle
    block = 4 * n    # Kanalnummern pro Block
    if band == "6":
        base = 1
    else:
        base = 149 if primary >= 149 else 36
    start = base + ((primary - base) // block) * block
    return start + 2 * (n - 1)


def channel_span(center_freq: float, width: int) -> tuple[float, float]:
    return center_freq - width / 2.0, center_freq + width / 2.0


def band_channels(band: str) -> list[int]:
    """20-MHz-Kanäle, die im Graphen beschriftet werden."""
    if band == "2.4":
        return list(range(1, 15))
    if band == "5":
        return list(range(36, 65, 4)) + list(range(100, 145, 4)) + list(range(149, 178, 4))
    if band == "6":
        return list(range(1, 234, 4))
    return []


def is_psc(channel: int) -> bool:
    """6-GHz Preferred Scanning Channel (5, 21, 37, ...)."""
    return channel >= 5 and (channel - 5) % 16 == 0
