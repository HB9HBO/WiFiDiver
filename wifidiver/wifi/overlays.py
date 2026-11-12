"""Kanalraster anderer Funkdienste innerhalb der WLAN-Bänder (Überlagerung im Kanalgraph).

Es werden nur Dienste berücksichtigt, die in den angezeigten WLAN-Bändern liegen und
WLAN stören bzw. im SDR-Wasserfall auftauchen können.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Channel:
    center: float     # MHz
    width: float      # MHz
    label: str = ""   # Kurzbezeichnung im Graph ("" = nur Raster)


@dataclass(frozen=True)
class Overlay:
    key: str
    name: str                     # Anzeigename (Checkbox)
    band: str                     # "2.4", "5", "6"
    color: tuple[int, int, int]
    style: str                    # "region": hervorgehobene Bereiche, "raster": Strichleiste unten
    channels: tuple[Channel, ...] = field(default_factory=tuple)
    hint: str = ""                # Tooltip
    prefix: str = ""              # für die Mauszeiger-Info, z.B. "BLE"
    tag: str = ""                 # Präfix der Beschriftung im Graph, z.B. "Z" -> "Z15"
    group_label: str = ""         # statt Einzelbeschriftung eine gemeinsame (schmale Kanäle)

    @property
    def lo(self) -> float:
        return min(c.center - c.width / 2 for c in self.channels)

    @property
    def hi(self) -> float:
        return max(c.center + c.width / 2 for c in self.channels)


def _bt_classic() -> tuple[Channel, ...]:
    return tuple(Channel(2402.0 + k, 1.0) for k in range(79))


BLE_ADV = {37: 2402.0, 38: 2426.0, 39: 2480.0}


def ble_channel_freq(ch: int) -> float:
    """BLE-Kanalnummer (0–39) -> Mittenfrequenz. 37/38/39 sind die Advertising-Kanäle."""
    if ch in BLE_ADV:
        return BLE_ADV[ch]
    if 0 <= ch <= 10:
        return 2404.0 + 2 * ch
    if 11 <= ch <= 36:
        return 2428.0 + 2 * (ch - 11)
    raise ValueError(f"ungültiger BLE-Kanal {ch}")


def zigbee_channel_freq(ch: int) -> float:
    """IEEE 802.15.4 (Zigbee/Thread/Matter) Kanal 11–26 -> Mittenfrequenz."""
    if not 11 <= ch <= 26:
        raise ValueError(f"ungültiger 802.15.4-Kanal {ch}")
    return 2405.0 + 5 * (ch - 11)


OVERLAYS: tuple[Overlay, ...] = (
    # --- 2.4 GHz ------------------------------------------------------------
    Overlay("bt_classic", "Bluetooth Classic (79 × 1 MHz)", "2.4", (80, 170, 255), "raster",
            _bt_classic(),
            "Frequenzsprungverfahren über 79 Kanäle à 1 MHz (2402–2480 MHz), ~1600 Sprünge/s",
            "BT"),
    Overlay("ble_data", "BLE Datenkanäle (37 × 2 MHz)", "2.4", (120, 220, 255), "raster",
            tuple(Channel(ble_channel_freq(c), 2.0) for c in range(37)),
            "Bluetooth Low Energy, Datenkanäle 0–36 (Frequenzsprung nach Verbindungsaufbau)",
            "BLE"),
    Overlay("ble_adv", "BLE Advertising (37/38/39)", "2.4", (0, 200, 255), "region",
            tuple(Channel(f, 2.0, str(c)) for c, f in BLE_ADV.items()),
            "Advertising-Kanäle 37/38/39 (2402/2426/2480 MHz) – absichtlich neben WLAN-Kanal 1/6/11",
            "BLE Adv.", "BLE "),
    Overlay("zigbee", "Zigbee / Thread (802.15.4)", "2.4", (120, 230, 90), "region",
            tuple(Channel(zigbee_channel_freq(c), 2.0, str(c)) for c in range(11, 27)),
            "IEEE 802.15.4, Kanäle 11–26 im 5-MHz-Raster, 2 MHz breit (Zigbee, Thread/Matter). "
            "15, 20, 25, 26 liegen zwischen WLAN 1/6/11.",
            "802.15.4", "Z"),
    Overlay("microwave", "Mikrowellenherd (typ. Bereich)", "2.4", (255, 140, 60), "region",
            (Channel(2455.0, 30.0, "Mikrowelle"),),
            "Magnetron um ~2450 MHz, gepulst im Takt der Netzfrequenz; Bereich ist geräteabhängig",
            "Mikrowelle"),
    # --- 5 GHz --------------------------------------------------------------
    Overlay("weather_radar", "Wetterradar (C-Band)", "5", (255, 90, 90), "region",
            (Channel(5625.0, 50.0, "Wetterradar"),),
            "5600–5650 MHz: Wetterradare; WLAN-Kanäle 120–128 hier mit langer DFS-Wartezeit bzw. gesperrt",
            "Wetterradar"),
    Overlay("its_g5", "ITS-G5 / 802.11p (Car2X)", "5", (255, 200, 60), "region",
            tuple(Channel(5855.0 + 10 * k + 5, 10.0, str(172 + 2 * k)) for k in range(7)),
            "Fahrzeugkommunikation 5855–5925 MHz, 10-MHz-Kanäle 172–184 (oberhalb 5895 MHz außerhalb der Achse)",
            "ITS-G5", group_label="ITS-G5"),
    Overlay("fpv", "FPV-Video 5.8 GHz (Raceband)", "5", (230, 110, 255), "region",
            tuple(Channel(f, 18.0, f"R{i + 1}")
                  for i, f in enumerate((5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917))),
            "Analoges Drohnen-Video, Raceband R1–R8, je ca. 18 MHz belegt",
            "FPV"),
    # --- 6 GHz --------------------------------------------------------------
    Overlay("uwb", "UWB (Kanal 5 / 6)", "6", (255, 170, 60), "region",
            (Channel(6489.6, 499.2, "UWB 5"), Channel(6988.8, 499.2, "UWB 6")),
            "Ultra-Wideband (IEEE 802.15.4z): Ortung, AirTags, Smartphones, Autoschlüssel. "
            "Sehr geringe Leistungsdichte, kurze Impulse",
            "UWB"),
)

OVERLAY_BY_KEY = {o.key: o for o in OVERLAYS}
DEFAULT_ON = ("ble_adv", "zigbee")


def overlays_for_band(band: str) -> list[Overlay]:
    return [o for o in OVERLAYS if o.band == band]


def describe(freq: float, band: str, enabled) -> list[str]:
    """Texte der aktivierten Überlagerungen an dieser Frequenz (für die Mauszeiger-Info)."""
    out = []
    for o in overlays_for_band(band):
        if o.key not in enabled:
            continue
        for c in o.channels:
            if abs(freq - c.center) <= c.width / 2:
                if o.style == "raster":
                    idx = o.channels.index(c)
                    out.append(f"{o.prefix} {idx}")
                else:
                    lbl = c.label
                    out.append(lbl if lbl.startswith(o.prefix) or lbl == o.name else f"{o.prefix} {lbl}".strip())
                break
    return out
