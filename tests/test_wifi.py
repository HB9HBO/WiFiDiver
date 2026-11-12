import unittest

import numpy as np

from wifidiver.sdr.base import resample_to_grid
from wifidiver.wifi.channels import channel_to_freq, freq_to_channel, standard_center_channel
from wifidiver.wifi.iw_parser import parse_scan_dump

DUMP = """\
BSS 3c:84:6a:aa:bb:01(on wlan0) -- associated
\tlast seen: 12345.678s [boottime]
\tTSF: 1234 usec (0d, 00:00:00)
\tfreq: 5180.0
\tbeacon interval: 100 TUs
\tcapability: ESS Privacy SpectrumMgmt (0x0111)
\tsignal: -52.00 dBm
\tlast seen: 120 ms ago
\tInformation elements from Probe Response frame:
\tSSID: Heim\\x20Netz
\tDS Parameter set: channel 36
\tBSS Load:
\t\t * station count: 4
\t\t * channel utilisation: 51/255
\t\t * available admission capacity: 0 [*32us]
\tHT capabilities:
\t\tCapabilities: 0x9ef
\tHT operation:
\t\t * primary channel: 36
\t\t * secondary channel offset: above
\t\t * STA channel width: any
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK SAE
\t\t * Capabilities: 16-PTKSA-RC 1-GTKSA-RC MFP-capable (0x00ac)
\tVHT capabilities:
\t\tVHT Capabilities (0x338b79b2):
\tVHT operation:
\t\t * channel width: 1 (80 MHz)
\t\t * center freq segment 1: 42
\t\t * center freq segment 2: 0
\t\t * VHT basic MCS set: 0xfffc
\tHE capabilities:
\t\tHE MAC Capabilities (0x000801185018):
BSS 00:11:22:33:44:55(on wlan0)
\tfreq: 2437
\tcapability: ESS (0x0401)
\tsignal: -78.00 dBm
\tlast seen: 2400 ms ago
\tSSID:
\tHT capabilities:
\t\tCapabilities: 0x1ad
\tHT operation:
\t\t * primary channel: 6
\t\t * secondary channel offset: below
BSS 00:11:22:33:44:66(on wlan0)
\tfreq: 5500
\tsignal: -70.00 dBm
\tSSID: Breit
\tcapability: ESS Privacy (0x0011)
\tRSN:\t * Version: 1
\t\t * Authentication suites: IEEE 802.1X
\tVHT operation:
\t\t * channel width: 1 (80 MHz)
\t\t * center freq segment 1: 106
\t\t * center freq segment 2: 114
"""


class ChannelTests(unittest.TestCase):
    def test_freq_channel_roundtrip(self):
        for band, ch in (("2.4", 1), ("2.4", 13), ("2.4", 14), ("5", 36), ("5", 165), ("6", 1), ("6", 233)):
            self.assertEqual(freq_to_channel(channel_to_freq(ch, band)), ch)

    def test_standard_centers(self):
        self.assertEqual(standard_center_channel(36, 80, "5"), 42)
        self.assertEqual(standard_center_channel(64, 80, "5"), 58)
        self.assertEqual(standard_center_channel(157, 80, "5"), 155)
        self.assertEqual(standard_center_channel(116, 160, "5"), 114)
        self.assertEqual(standard_center_channel(44, 40, "5"), 46)
        self.assertEqual(standard_center_channel(37, 160, "6"), 47)
        self.assertEqual(standard_center_channel(5, 320, "6"), 31)
        self.assertEqual(standard_center_channel(6, 40, "2.4", "below"), 4)


class IwParserTests(unittest.TestCase):
    def setUp(self):
        self.aps = {ap.bssid: ap for ap in parse_scan_dump(DUMP)}

    def test_count(self):
        self.assertEqual(len(self.aps), 3)

    def test_vht80(self):
        ap = self.aps["3c:84:6a:aa:bb:01"]
        self.assertEqual(ap.ssid, "Heim Netz")
        self.assertEqual(ap.channel, 36)
        self.assertEqual(ap.width, 80)
        self.assertEqual(ap.center_freq, 5210)
        self.assertEqual(ap.signal, -52)
        self.assertEqual(ap.stations, 4)
        self.assertAlmostEqual(ap.utilization, 20.0, places=0)
        self.assertEqual(ap.security, "WPA2/WPA3-Personal")
        self.assertEqual(ap.generation, "Wi-Fi 6")
        self.assertTrue(ap.connected)
        self.assertEqual(ap.last_seen_ms, 120)

    def test_ht40_below_hidden_open(self):
        ap = self.aps["00:11:22:33:44:55"]
        self.assertEqual(ap.display_ssid, "<versteckt>")
        self.assertEqual(ap.width, 40)
        self.assertEqual(ap.center_freq, 2427)
        self.assertEqual(ap.security, "Offen")
        self.assertEqual(ap.generation, "Wi-Fi 4")

    def test_vht160_via_seg2(self):
        ap = self.aps["00:11:22:33:44:66"]
        self.assertEqual(ap.width, 160)
        self.assertEqual(ap.center_freq, channel_to_freq(114, "5"))
        self.assertEqual(ap.security, "WPA2-Enterprise")


class ResampleTests(unittest.TestCase):
    def test_max_and_interp(self):
        f = np.array([10.1, 10.2, 10.6, 12.5])
        p = np.array([-50.0, -40.0, -60.0, -70.0])
        g = resample_to_grid(f, p, 10.0, 13.0, 3)
        self.assertEqual(g[0], -40.0)          # Maximum im ersten Bin
        self.assertEqual(g[2], -70.0)
        self.assertTrue(-70.0 <= g[1] <= -40.0)  # interpoliert


if __name__ == "__main__":
    unittest.main()
