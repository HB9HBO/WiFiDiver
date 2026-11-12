"""Datenmodell für ein gefundenes Access Point (BSS)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .channels import band_of_freq, channel_to_freq, freq_to_channel, standard_center_channel


@dataclass
class AccessPoint:
    bssid: str
    ssid: str = ""
    freq: float = 0.0             # Primärfrequenz MHz
    signal: float = -100.0        # dBm
    width: int = 20               # MHz
    center_freq: float = 0.0      # MHz (Mitte des belegten Kanals)
    security: str = "Offen"
    generation: str = ""          # "Wi-Fi 4" ... "Wi-Fi 7"
    utilization: float | None = None   # 0..100 %
    stations: int | None = None
    last_seen_ms: int | None = None
    vendor: str = ""
    connected: bool = False
    ht_offset: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def band(self) -> str | None:
        return band_of_freq(self.freq)

    @property
    def channel(self) -> int:
        return freq_to_channel(self.freq)

    @property
    def display_ssid(self) -> str:
        return self.ssid if self.ssid else "<versteckt>"

    def finalize(self) -> "AccessPoint":
        """Fehlende Mittenfrequenz aus dem Standardraster ergänzen."""
        band = self.band
        if band is None:
            return self
        if band == "2.4" and self.width > 40:
            self.width = 40
        if not self.center_freq:
            cch = standard_center_channel(self.channel, self.width, band, self.ht_offset)
            self.center_freq = channel_to_freq(cch, band)
        return self
