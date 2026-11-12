import os
import tempfile
import time
import unittest

from wifidiver.wifi.connection import (ConnectionView, LinkInfo, describe_bitrate, parse_link,
                                       parse_station_dump, parse_survey)
from wifidiver.wifi.details import ap_details, max_phy_rate
from wifidiver.wifi.iw_parser import parse_scan_dump
from wifidiver.wifi.model import AccessPoint

DUMP = """\
BSS d4:bd:4f:49:56:70(on wlan0) -- associated
\tlast seen: 174214.392s [boottime]
\tfreq: 5180.0
\tbeacon interval: 100 TUs
\tcapability: ESS Privacy SpectrumMgmt RadioMeasure (0x1111)
\tsignal: -48.00 dBm
\tlast seen: 76 ms ago
\tInformation elements from Beacon frame:
\tSSID: Heimnetz
\tSupported rates: 6.0* 9.0 12.0* 18.0 24.0* 36.0 48.0 54.0
\tTIM: DTIM Count 0 DTIM Period 3 Bitmap Control 0x0 Bitmap[0] 0x0
\tCountry: CH\tEnvironment: Indoor/Outdoor
\t\tChannels [36 - 48] @ 23 dBm
\tTPC report: TX power: 20 dBm
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: SAE FT/SAE
\t\t * Capabilities: 1-PTKSA-RC 1-GTKSA-RC MFP-required MFP-capable (0x00cc)
\tBSS Load:
\t\t * station count: 7
\t\t * channel utilisation: 51/255
\t\t * available admission capacity: 0 [*32us]
\tMobility Domain: MDID: 0x1234 Over-the-DS
\tRM enabled capabilities:
\t\tCapabilities: 0x73 0xd0 0x00 0x00 0x0c
\t\t\tNeighbor Report
\tHT capabilities:
\t\tCapabilities: 0x9ef
\t\t\tRX LDPC
\t\t\tRX HT20 SGI
\t\tHT TX/RX MCS rate indexes supported: 0-31
\tHT operation:
\t\t * primary channel: 36
\t\t * secondary channel offset: above
\tVHT capabilities:
\t\tVHT RX MCS set:
\t\t\t1 streams: MCS 0-9
\t\t\t2 streams: MCS 0-9
\t\t\t3 streams: MCS 0-9
\t\t\t4 streams: MCS 0-9
\t\t\t5 streams: not supported
\tVHT operation:
\t\t * channel width: 1 (80 MHz)
\t\t * center freq segment 1: 42
\t\t * center freq segment 2: 0
\tExtended capabilities:
\t\t * BSS Transition
\tHE capabilities:
\t\tHE MAC Capabilities (0x010d1a080040):
\t\t\tTWT Responder
\t\tHE PHY Capabilities: (0x007008881d418104110800):
\t\t\tSU Beamformer
\t\tHE RX MCS and NSS set <= 80 MHz
\t\t\t1 streams: MCS 0-11
\t\t\t2 streams: MCS 0-11
\t\t\t3 streams: MCS 0-11
\t\t\t4 streams: MCS 0-11
\t\t\t5 streams: not supported
\tVendor specific: OUI 00:10:18, data: 02 00 00 1c 00 00
\tWMM:\t * Parameter version 1
"""

LINK = """\
Connected to d4:bd:4f:49:56:70 (on wlan0)
\tSSID: Heimnetz
\tfreq: 5180.0
\tRX: 1234567 bytes (8901 packets)
\tTX: 123456 bytes (789 packets)
\tsignal: -48 dBm
\trx bitrate: 1200.9 MBit/s 80MHz HE-MCS 11 HE-NSS 2 HE-GI 0 HE-DCM 0
\ttx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
\tbss flags: short-slot-time
\tdtim period: 3
\tbeacon int: 100
"""

STATION = """\
Station d4:bd:4f:49:56:70 (on wlan0)
\tinactive time:\t12 ms
\trx bytes:\t{rx}
\trx packets:\t8901
\ttx bytes:\t123456
\ttx packets:\t{tp}
\ttx retries:\t{re}
\ttx failed:\t2
\trx drop misc:\t5
\tsignal:  \t-48 [-50, -51] dBm
\tsignal avg:\t-47 [-49, -50] dBm
\ttx bitrate:\t866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
\trx bitrate:\t1200.9 MBit/s 80MHz HE-MCS 11 HE-NSS 2 HE-GI 0 HE-DCM 0
\texpected throughput:\t455.62Mbps
\tauthorized:\tyes
\tconnected time:\t3725 seconds
"""

SURVEY = """\
Survey data from wlan0
\tfrequency:\t\t\t5170 MHz
\tnoise:\t\t\t\t-96 dBm
Survey data from wlan0
\tfrequency:\t\t\t5180 MHz [in use]
\tnoise:\t\t\t\t-92 dBm
\tchannel active time:\t\t{act} ms
\tchannel busy time:\t\t{busy} ms
Survey data from wlan0
\tfrequency:\t\t\t5200 MHz
"""


def rows(sections):
    return {label: value for _, rs in sections for label, value in rs}


class DetailsTest(unittest.TestCase):
    def setUp(self):
        self.ap = parse_scan_dump(DUMP)[0]

    def test_details_fields(self):
        r = rows(ap_details(self.ap))
        self.assertEqual(r["DTIM-Periode"], "3")
        self.assertEqual(r["Land (802.11d)"], "CH  Indoor/Outdoor")
        self.assertEqual(r["Sendeleistung (TPC)"], "20 dBm")
        self.assertEqual(r["PMF / 802.11w"], "erforderlich")
        self.assertEqual(r["AKM (Authentifizierung)"], "SAE FT/SAE")
        self.assertEqual(r["Räumliche Ströme (MIMO)"], "4")
        self.assertEqual(r["802.11k (Neighbor Report)"], "ja")
        self.assertEqual(r["802.11v (BSS Transition)"], "ja")
        self.assertIn("MDID", r["802.11r (Fast Transition)"])
        self.assertEqual(r["Clients (BSS Load)"], "7")
        self.assertIn("00:10:18 (Broadcom)", r["OUIs"])
        self.assertIn("00:50:f2", r["OUIs"])
        self.assertIn("2'402 Mbit/s", r["Max. PHY-Rate (geschätzt)"])    # HE 80 MHz × 4
        self.assertEqual(r["Basisraten"], "6 12 24 Mbit/s")
        self.assertEqual(r["Verbunden"], "ja (dieses Gerät)")

    def test_without_raw_data(self):
        ap = AccessPoint("aa:bb:cc:dd:ee:ff", "X", 2412, -60, 20).finalize()
        r = rows(ap_details(ap))
        self.assertIn("Hinweis", r)

    def test_max_rate(self):
        self.assertAlmostEqual(max_phy_rate("Wi-Fi 5", 80, 2), 866.6, places=1)
        self.assertAlmostEqual(max_phy_rate("Wi-Fi 4", 80, 1), 150.0)   # HT max. 40 MHz
        self.assertIsNone(max_phy_rate("Legacy", 20, 1))


class ConnectionTest(unittest.TestCase):
    def info(self, ts, rx, tp, re_, act, busy):
        i = parse_link(LINK, "wlan0")
        i.station = parse_station_dump(STATION.format(rx=rx, tp=tp, re=re_), i.bssid)
        i.survey = parse_survey(SURVEY.format(act=act, busy=busy))
        i.ts = ts
        return i

    def test_parse(self):
        i = self.info(0, 1000, 100, 10, 1000, 300)
        self.assertTrue(i.connected)
        self.assertEqual(i.bssid, "d4:bd:4f:49:56:70")
        self.assertEqual(i.ssid, "Heimnetz")
        self.assertEqual(i.freq, 5180.0)
        self.assertEqual(i.survey["noise"], "-92 dBm")
        self.assertEqual(i.station["tx retries"], "10")
        self.assertFalse(parse_link("Not connected.", "wlan0").connected)

    def test_view_rates_and_roaming(self):
        view = ConnectionView()
        aps = parse_scan_dump(DUMP)
        other = AccessPoint("d4:bd:4f:49:56:99", "Heimnetz", 2437, -60, 20).finalize()
        aps.append(other)
        view.sections(self.info(100.0, 1_000_000, 1000, 50, 10_000, 2_000), aps)
        title, sec = view.sections(self.info(102.0, 3_000_000, 1200, 70, 12_000, 3_000), aps)
        r = rows(sec)
        self.assertEqual(title, "Verbunden mit Heimnetz")
        self.assertEqual(r["SNR"], "44 dB")
        self.assertEqual(r["Signal pro Antenne"], "-50 / -51 dBm")
        self.assertEqual(r["Kanal belegt"], "50 %  (eigene Messung)")
        self.assertIn("↓ 8.00", r["Aktueller Verkehr"])
        self.assertEqual(r["TX-Wiederholungen aktuell"], "10.0 %")
        self.assertEqual(r["Verbunden seit"], "1 h 02 min")
        self.assertIn("-12 dB", r["d4:bd:4f:49:56:99"])
        # Neu anzeigen ohne neue Abfrage behält die Raten
        title2, sec2 = view.sections(view.last, aps)
        self.assertEqual(rows(sec2)["Kanal belegt"], "50 %  (eigene Messung)")

    def test_rows_stable_without_traffic(self):
        view = ConnectionView()
        _, first = view.sections(self.info(1.0, 1000, 100, 10, 1000, 300), [])
        _, idle = view.sections(self.info(3.0, 1000, 100, 10, 2000, 400), [])   # nichts gesendet
        self.assertEqual([l for _, r in first for l, _ in r], [l for _, r in idle for l, _ in r])
        self.assertEqual(rows(idle)["TX-Wiederholungen aktuell"], "– (keine Pakete gesendet)")
        self.assertEqual(rows(first)["TX-Wiederholungen aktuell"], "– (wird gemessen)")

    def test_not_connected(self):
        title, sec = ConnectionView().sections(LinkInfo("wlan0"), [])
        self.assertIn("Nicht verbunden", title)
        self.assertEqual(sec, [])

    def test_bitrate_text(self):
        self.assertEqual(describe_bitrate("1200.9 MBit/s 80MHz HE-MCS 11 HE-NSS 2 HE-GI 0 HE-DCM 0"),
                         "1200.9 Mbit/s (802.11ax, MCS 11, 2 Ströme, 80 MHz, GI 0,8 µs)")
        self.assertEqual(describe_bitrate("144.4 MBit/s MCS 15 short GI"),
                         "144.4 Mbit/s (802.11n, MCS 15, 2 Ströme, kurzes GI)")
        self.assertEqual(describe_bitrate("54.0 MBit/s"), "54 Mbit/s")


class SsidListTest(unittest.TestCase):
    def test_patterns_and_modes(self):
        from PyQt6.QtCore import QSettings
        from wifidiver.ui.ssid_list import SsidList
        with tempfile.TemporaryDirectory() as d:
            st = QSettings(os.path.join(d, "t.ini"), QSettings.Format.IniFormat)
            lst = SsidList(st)
            lst.set_entries(["Heimnetz", "FRITZ!Box*", "Heimnetz"])
            self.assertEqual(lst.entries, ["Heimnetz", "FRITZ!Box*"])
            self.assertTrue(lst.matches("FRITZ!Box 7590"))
            self.assertFalse(lst.matches("fritz!box 7590"))
            self.assertFalse(lst.matches(""))
            self.assertTrue(lst.accepts("Nachbar", "hide"))
            self.assertFalse(lst.accepts("Heimnetz", "hide"))
            self.assertTrue(lst.accepts("Heimnetz", "only"))
            self.assertFalse(lst.accepts("Nachbar", "only"))
            st.sync()
            self.assertEqual(SsidList(QSettings(os.path.join(d, "t.ini"),
                                                QSettings.Format.IniFormat)).entries,
                             ["Heimnetz", "FRITZ!Box*"])
            single = QSettings(os.path.join(d, "s.ini"), QSettings.Format.IniFormat)
            single.setValue(SsidList.KEY, "Nur-eins")
            self.assertEqual(SsidList(single).entries, ["Nur-eins"])


if __name__ == "__main__":
    unittest.main()
