# WifiDiver

WLAN-Analyzer für Linux im Stil von [WaveScope](https://github.com/yurividal/WaveScope).
Zusätzlich zeigt der Kanalgraph das echte HF-Spektrum und einen **Wasserfall eines SDR**
(HackRF One oder UHD/USRP), sodass man die Beacons/Bursts der APs direkt sieht.

## Funktionen

- AP-Tabelle: SSID, BSSID, Hersteller (OUI), Band, Kanal (inkl. DFS), Breite, Mittenfrequenz,
  Signal, Sicherheit (WPA2/WPA3/OWE/Enterprise/802.11r), Wi-Fi-Generation, Kanalauslastung,
  Clients, zuletzt gesehen. Sortieren und Textfilter, optional nur aktuelles Band.
- Kanalgraph pro Band (2.4 / 5 / 6 GHz) mit AP-Kurven über die tatsächliche Kanalbreite
  (20–320 MHz), DFS-Bereich und 6-GHz-PSC-Markierungen. Klick auf eine Kurve wählt den AP aus.
- **SDR-Spektrum** (Live, Peak-Hold, Mittelwert) im Kanalgraph und **Wasserfall** darunter,
  mit gemeinsamer Frequenzachse (Zoom/Pan synchron). Der Kanal des ausgewählten AP wird im
  Wasserfall markiert. Zeitachse in Sekunden, Mauszeiger zeigt Frequenz/Kanal/Pegel.
- **Andere Funkdienste in den WLAN-Bändern** als umschaltbare Überlagerung im Kanalgraph (SDR-Dock):
  2.4 GHz: Bluetooth Classic (79 × 1 MHz) und BLE-Datenkanäle als Strichleiste, BLE-Advertising 37/38/39,
  Zigbee/Thread (802.15.4, Kanal 11–26), Mikrowellenherd (typ. Bereich); 5 GHz: Wetterradar (5600–5650 MHz),
  ITS-G5/802.11p, FPV-Video (Raceband); 6 GHz: UWB Kanal 5/6. Der Mauszeiger nennt die Kanäle an der Position.
  Es sind reine Kanalraster; die Signale selbst ordnet die *Signalerkennung* zu (siehe unten).
- **Automatische Signalerkennung** im Wasserfall (SDR-Dock, *Signalerkennung*): WLAN (mit SSID des APs),
  BLE-Advertising, Bluetooth-Frequenzsprung, Zigbee/Thread, Mikrowellenherd, FPV-Video, ITS-G5, Radar-Pulse,
  Dauerträger/Störlinien. Ergebnisliste der letzten 5 s, farbige Markierungen im Wasserfall (Mauszeiger
  darauf zeigt Frequenz, Breite, Dauer, Pegel). Keine Dekodierung, nur Frequenzraster, Breite, Dauer und
  Wiederholung.
- **Details-Tab** zum ausgewählten AP: aufbereitete Informationselemente der Beacons – Standard, räumliche
  Ströme, geschätzte max. PHY-Rate, Basisraten, PHY-Merkmale (Beamforming, MU-MIMO, TWT, 1024-QAM),
  BSS-Color, Sendeleistung (TPC, 802.11d-Grenzwerte), Sicherheit (Cipher, AKM, PMF/802.11w, WPS),
  Roaming (802.11k/v/r), BSS Load und Hersteller-IEs; dazu die Rohdaten von `iw`.
- **Verbindung-Tab**: aktuelle WLAN-Verbindung (erstes verbundene Interface) mit Signal pro Antenne, Rauschen,
  SNR, Kanalbelegung, RX/TX-Rate (MCS, Ströme, Breite, GI), erwartetem Durchsatz, aktuellem Verkehr,
  TX-Wiederholungen/Fehlern und Roaming-Kandidaten (gleiche SSID). Quelle: `iw link`, `station dump`,
  `survey dump` (ohne root).
- **SSID-Liste**: eigene/bekannte Netze markieren (★, Rechtsklick in der Tabelle oder *Liste …*, Muster mit
  `*` und `?`), Filter *Nur Liste* bzw. *Liste ausblenden* für Tabelle, Kanalgraph und Signalverlauf.
  Die Signalerkennung berücksichtigt weiterhin alle APs.
- Signalverlauf (2 Minuten) pro BSSID.
- Demo-Modus für WLAN und SDR (zum Testen ohne Hardware).

## Voraussetzungen (Debian/Ubuntu)

```
sudo apt install python3-pyqt6 python3-pyqtgraph python3-numpy iw ieee-data \
                 hackrf python3-uhd uhd-host
sudo uhd_images_downloader   # FPGA-/Firmware-Images für USRP
```

## Start

```
./wifidiver.sh                        # automatisch: iw (oder nmcli), SDR aus Einstellungen
./wifidiver.sh --sdr HackRF --start   # HackRF-Sweep sofort starten
./wifidiver.sh --sdr UHD --band 5 --start
./wifidiver.sh --source demo --sdr Demo --start   # komplett ohne Hardware
```

Tests: `python3 -m unittest discover -s tests -t .`

## WLAN-Daten

- **iw** (Standard): liest den Kernel-Scan-Cache (`iw dev <if> scan dump`, ohne root).
  Neue Scans werden über NetworkManager (`nmcli dev wifi rescan`) ausgelöst. Läuft kein
  NetworkManager, braucht `iw scan trigger` die Capability `CAP_NET_ADMIN`, z.B.:
  `sudo setcap cap_net_admin+ep "$(readlink -f "$(command -v iw)")"` – oder die App mit sudo starten.
  Das Interface muss `up` sein (`sudo ip link set wlan0 up`).
- **nmcli**: nur NetworkManager. Hier wird die Mittenfrequenz breiter Kanäle aus dem Standard-Raster geschätzt.

## SDR

Gesweept wird immer das Band des aktiven Tabs; ein Tabwechsel startet den Sweep neu.

**Sweep-Bereich:** Im SDR-Dock lässt sich der Sweep mit zwei Slidern (min/max, mind. 5 MHz) auf einen Teil
des Bandes beschränken, z.B. um einzelne Kanäle genauer zu untersuchen. Schnellwahl: *Ganzes Band*,
*Ausgew. AP* (Kanal des ausgewählten AP mit Rand) und *Sichtbereich* (aktueller Zoom des Kanalgraphen).
Der Bereich wird je Band gespeichert und im Kanalgraph blau markiert; das Wasserfallraster (1500 Punkte)
folgt ihm, die Auflösung steigt also. Schmale Bereiche sweepen deutlich schneller (B210, 80 MHz: ~120/s;
20 MHz: ~1000/s ohne Umstimmen; HackRF, 80 MHz: ~100/s). Alle Sweeps eines Anzeigetakts (40 ms) werden
per Maximum zu einer Wasserfallzeile zusammengefasst. Für feinere Auflösung FFT-Größe (UHD) bzw.
Bin-Breite (HackRF) anpassen.

| Gerät | Umsetzung | Bereich | Typische Rate |
|---|---|---|---|
| HackRF One | `hackrf_sweep -B` (Subprozess, Binärausgabe) | 1–7250 MHz | 2.4 GHz: ~80 Sweeps/s, 5 GHz: ~11/s |
| UHD (B2xx, N2xx, X3xx …) | Python-`uhd` in eigenem Prozess, schrittweises Abstimmen und Zusammensetzen der FFTs | geräteabhängig (B210: 70–6000 MHz) | B210 an USB 3, 56 MS/s: 2.4 GHz ~80/s, 5 GHz ~3/s |

Hinweise:
- Die SDR-Werte sind **nicht kalibriert** (HackRF: dB, UHD: dBFS). Mit dem *Pegel-Offset* lassen sie sich
  grob auf die dBm-Skala der APs legen. *Auto-Pegel* setzt die Wasserfallfarben anhand der Daten.
- B210: Jedes Umstimmen des AD9361 dauert ~30–40 ms – das begrenzt die 5-GHz-Sweeprate. Höhere Abtastrate
  (= weniger Schritte) und USB 3 helfen. Über USB 2 die Abtastrate auf ≤ 40 MS/s senken.
- Fehlt `UHD_IMAGES_DIR` (z.B. unter `sudo`, das die Umgebung zurücksetzt), sucht WifiDiver die Images selbst
  (`/usr/share/uhd/images`, `/usr/share/uhd/<version>/images`, …).
- Das USRP läuft in einem eigenen Prozess, weil die UHD-Bindings beim Umstimmen den Python-GIL halten;
  im GUI-Prozess würde sonst die Oberfläche (und damit der Wasserfall) bei vielen Schritten einfrieren. Das erste Öffnen lädt das FPGA (über USB 2 bis zu ~1–2 min).
- Die genutzte Teilbandbreite pro Schritt (Standard 75 % der Rate) vermeidet die Filterflanken; die DC-Spitze
  in der Bandmitte wird interpoliert.
- HackRF zeigt eigene Spurs (z.B. bei 2400/2440/2480 MHz).
- HackRF braucht ~320 Mbit/s auf USB 2. Hängt er am selben USB-Hub wie der WLAN-Stick, bricht der Datenstrom
  beim WLAN-Scan ab (`streaming stopped (-1003)`). WifiDiver startet `hackrf_sweep` dann automatisch neu
  (Anzeige „× neu gestartet“); besser den HackRF an einen eigenen Port/Controller anschließen.
- Das 6-GHz-Band reicht beim B210 nur bis 6 GHz.

### CPU-Last (Raspberry Pi, ältere Notebooks)

- **Bildrate** (SDR-Dock, *Darstellung*): 25/15/10/5 Bilder/s. Bestimmt nur, wie oft Spektrum und Wasserfall
  neu gezeichnet werden; Daten und Signalerkennung laufen immer mit 25 Zeilen/s weiter. 10 Bilder/s halbiert
  die CPU-Last der Oberfläche etwa.
- **Max. Sweeps/s** (UHD-Parameter, 0 = frei): Das USRP sweept sonst so schnell es kann (2.4 GHz ~80/s), der
  größte Teil der Last steckt dann in UHD/USB. Mit 25 Sweeps/s sinkt die Last des USRP-Prozesses auf etwa ein
  Drittel; kurze Bursts werden dafür etwas seltener erfasst.
- Weniger *Verlauf*-Zeilen und ein schmalerer *Sweep-Bereich* entlasten zusätzlich.

Messung (Desktop-CPU, 2.4 GHz, ganzes Band, 25 Bilder/s → 10 Bilder/s, Anteil eines Kerns):

| | vorher | jetzt (25 / 10 Bilder/s) |
|---|---|---|
| HackRF, Oberfläche | 70 % | 24 % / 12 % |
| USRP B210, Oberfläche + USRP-Prozess | 36 % + 48 % | 8 % + 46 % (mit max. 25 Sweeps/s: 8 % + 15 %) |

Intern: der Wasserfall wird aus einem Ringpuffer gezeichnet und nur neue Zeilen werden eingefärbt; statische
Elemente des Kanalgraphen (AP-Kurven, Overlays, Achsen) werden zwischengespeichert; die Zuordnung der Sweep-Bins
zum Anzeigeraster wird einmal berechnet und wiederverwendet; `hackrf_sweep` liefert Binärdaten statt Text.

### Signalerkennung

Jede Wasserfallzeile wird gegen einen gleitenden Rauschboden (Median je Frequenz über die letzten ~64 Zeilen)
geprüft. Bereiche, die um mehr als die *Schwelle* (Standard 10 dB) darüber liegen, werden über
aufeinanderfolgende Zeilen zu Bursts verfolgt und eingeordnet:

| Klasse | Merkmal |
|---|---|
| WLAN | flaches OFDM-Spektrum im Kanal eines gescannten APs (bzw. auf dem 20/40/80/160-MHz-Raster) |
| BLE Advertising | schmal, gehäuft auf 2402/2426/2480 MHz |
| Bluetooth (Frequenzsprung) | schmal, viele verschiedene, jeweils selten belegte Frequenzen außerhalb aktiver WLAN-Kanäle |
| Zigbee / Thread | ~2 MHz, gehäuft genau auf einem 802.15.4-Kanal (2405 + 5·n MHz), außerhalb aktiver WLAN-Kanäle |
| Mikrowellenherd | breit, ungleichmäßig (nicht flach), 2425–2495 MHz, wiederholt bzw. andauernd |
| FPV-Video (analog) | andauernd, 6–30 MHz breit, auf einer 5,8-GHz-FPV-Frequenz (Bänder A/B/E/F/R) |
| ITS-G5 / Car2X | ~10 MHz breit auf einem ITS-G5-Kanal (5860–5920 MHz) |
| Radar-Pulse | sehr kurz und schmal, wiederholt auf derselben Frequenz im DFS-Bereich, abseits aktiver WLAN-Kanäle |
| Dauerträger / Störlinie | schmale Linie, die ständig anliegt (steckt schon im Rauschboden) |

Grenzen, die man kennen sollte:
- Ein Sweep-SDR sieht einen kurzen Burst nur, wenn es gerade auf dessen Frequenz steht. Raten sind daher
  relativ; seltene Signale (Radar, Zigbee) tauchen evtl. erst nach einiger Zeit auf.
- `hackrf_sweep` erfasst das Band in 5-MHz-Streifen nacheinander. Ein WLAN-Frame von wenigen ms erscheint
  deshalb oft nur als 5-MHz-Stück oder schmaler Rest. Solche Stücke im Kanal eines gescannten APs werden dem
  WLAN zugeordnet – Bluetooth und Zigbee werden in aktiven WLAN-Kanälen deshalb **nicht** gezählt. Mit dem
  USRP (56 MHz am Stück) sind WLAN-Frames vollständig sichtbar und die Einordnung ist sauberer.
- Teilen sich mehrere Netze einen Kanal, nennt die Liste alle (stärkstes zuerst) – welches Netz einen
  einzelnen Frame gesendet hat, lässt sich ohne Dekodierung nicht sagen.
- Der HackRF erzeugt eigene Störlinien (2.4 GHz: 2400/2440 MHz, 5 GHz: über 10 Linien). Sie erscheinen als
  *Dauerträger / Störlinie*. Bleibt eine Linie ohne Antenne bestehen, stammt sie aus dem SDR.

## Aufbau

```
wifidiver/
  wifi/   channels.py (Kanal/Frequenz-Mathematik), iw_parser.py, scanners.py (iw/nmcli/Demo), oui.py,
          overlays.py (Kanalraster anderer Funkdienste), details.py (Details-Tab),
          connection.py (Verbindung-Tab)
  sdr/    base.py (Thread, PSD, Raster-Resampling), hackrf.py, uhd_sdr.py, demo.py,
          classify.py (Signalerkennung)
  ui/     main_window.py, band_panel.py (Kanalgraph+Wasserfall), ap_table.py, history.py, sdr_panel.py,
          info_page.py (Details/Verbindung), ssid_list.py
```

Neue SDRs: Klasse von `SdrBackend` ableiten, `params` und `run()` implementieren
(Sweeps per `self._emit(Sweep(...))`), in `wifidiver/sdr/__init__.py` registrieren.

## Lizenz

GPL-3.0 – siehe [LICENSE](LICENSE).
