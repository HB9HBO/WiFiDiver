import unittest

import numpy as np

from wifidiver.sdr.classify import (SignalClassifier, fpv_channel, its_channel, wlan_match)
from wifidiver.wifi.model import AccessPoint

BINS = 1500


def grid(lo, hi):
    return lo + (np.arange(BINS) + 0.5) * (hi - lo) / BINS


class Sim:
    """Synthetische Spektrumszeilen: Rauschen −95 dB plus eingetragene Signale."""

    def __init__(self, band, lo, hi, dt=0.04, seed=1):
        self.g = grid(lo, hi)
        self.clf = SignalClassifier(band)
        self.rng = np.random.default_rng(seed)
        self.t = 1000.0
        self.dt = dt

    def row(self):
        return -95.0 + self.rng.normal(0, 1.0, BINS)

    @staticmethod
    def narrow(r, g, fc, half=0.5, lvl=-65.0):
        d = np.abs(g - fc) - half
        r[:] = np.maximum(r, lvl - np.maximum(d, 0) * 15.0)

    @staticmethod
    def ofdm(r, g, fc, width, lvl=-60.0):
        d = np.abs(g - fc) - (width / 2 - 0.5)
        r[:] = np.maximum(r, lvl - np.maximum(d, 0) * 25.0)

    def feed(self, r):
        self.clf.feed(self.g, r, self.t)
        self.t += self.dt

    def quiet(self, n=20):
        for _ in range(n):
            self.feed(self.row())

    def classes(self):
        return {f.key: f for f in self.clf.findings(self.t)}


class HelperTest(unittest.TestCase):
    def test_wlan_match(self):
        self.assertEqual(wlan_match("2.4", 2437, 19), 20)
        self.assertEqual(wlan_match("5", 5210, 78), 80)
        self.assertEqual(wlan_match("6", 6185, 158), 160)
        self.assertIsNone(wlan_match("2.4", 2437, 6))
        self.assertIsNone(wlan_match("5", 5300, 80))     # kein 80-MHz-Kanal dort

    def test_fpv_its(self):
        self.assertEqual(fpv_channel(5733), "B1")
        self.assertEqual(fpv_channel(5658), "R1")
        self.assertIsNone(fpv_channel(5500))
        self.assertEqual(its_channel(5900), 180)
        self.assertIsNone(its_channel(5700))


class ClassifierTest(unittest.TestCase):
    def test_ble_advertising(self):
        s = Sim("2.4", 2400, 2495)
        s.quiet()
        for i in range(60):
            r = s.row()
            if i % 4 == 0:
                for fc in (2402, 2426, 2480):
                    s.narrow(r, s.g, fc, 0.8)
            s.feed(r)
        found = s.classes()
        self.assertIn("ble_adv", found)
        self.assertIn("37", found["ble_adv"].where)
        self.assertNotIn("zigbee", found)

    def test_bluetooth_hopping_and_zigbee(self):
        s = Sim("2.4", 2400, 2495)
        s.quiet()
        rng = np.random.default_rng(7)
        for i in range(120):
            r = s.row()
            for _ in range(2):
                fc = 2402 + int(rng.integers(0, 79))
                if abs(fc - 2425) > 2:
                    s.narrow(r, s.g, fc, 0.4)
            if i % 3 == 0:
                s.narrow(r, s.g, 2425.0, 1.0, -70)     # 802.15.4 Kanal 15
            s.feed(r)
        found = s.classes()
        self.assertIn("bt", found)
        self.assertIn("zigbee", found)
        self.assertIn("Z15", found["zigbee"].where)

    def test_wlan_known_ap(self):
        s = Sim("2.4", 2400, 2495)
        s.clf.set_aps([AccessPoint("aa", "Heim", 2437, -55, 20).finalize()])
        s.quiet()
        for i in range(40):
            r = s.row()
            if i % 5 < 2:
                s.ofdm(r, s.g, 2437, 20)
            s.feed(r)
        found = s.classes()
        self.assertIn("wlan", found)
        self.assertIn("Heim", found["wlan"].where)
        self.assertEqual(set(found), {"wlan"})

    def test_continuous_carrier_despite_floor_tracking(self):
        s = Sim("2.4", 2400, 2495)
        s.quiet()
        for _ in range(150):         # 6 s – länger als das Rauschboden-Fenster
            r = s.row()
            s.narrow(r, s.g, 2450.3, 0.3, -70)
            s.feed(r)
        found = s.classes()
        self.assertIn("carrier", found)
        self.assertTrue(found["carrier"].active)

    def test_fpv_continuous(self):
        s = Sim("5", 5150, 5895)
        s.quiet()
        for _ in range(100):
            r = s.row()
            d = np.abs(s.g - 5658)
            r[:] = np.maximum(r, np.where(d < 9, -70 - d * 0.6, -200))
            s.feed(r)
        found = s.classes()
        self.assertIn("fpv", found)
        self.assertIn("R1", found["fpv"].where)
        self.assertNotIn("wlan", found)

    def test_wlan_fragments_in_known_channel(self):
        # Sweep-SDR erfasst kurze Frames nur in 5-MHz-Streifen bzw. als schmale Reste
        s = Sim("2.4", 2400, 2495)
        s.clf.set_aps([AccessPoint("aa", "Heim", 2412, -55, 20).finalize()])
        s.quiet()
        rng = np.random.default_rng(3)
        for i in range(80):
            r = s.row()
            if i % 2 == 0:
                lo = 2405.0 if i % 4 == 0 else 2415.0
                s.ofdm(r, s.g, lo + 2.5, 5.0)
            if i % 3 == 0:
                s.narrow(r, s.g, 2402.5 + 18 * rng.random(), 0.4)
            s.feed(r)
        found = s.classes()
        self.assertIn("wlan", found)
        self.assertIn("Heim", found["wlan"].where)
        self.assertNotIn("bt", found)
        self.assertNotIn("zigbee", found)

    def test_fixed_frequency_is_not_hopping(self):
        s = Sim("2.4", 2400, 2495)
        s.quiet()
        for i in range(120):
            r = s.row()
            if i % 2 == 0:
                s.narrow(r, s.g, 2459.3, 0.3)
            s.feed(r)
        found = s.classes()
        self.assertNotIn("bt", found)
        self.assertIn("narrow", found)
        self.assertIn("2459", found["narrow"].where)

    def test_floor_ripple_is_no_steady_signal(self):
        s = Sim("5", 5150, 5895)
        ripple = 6.0 * np.sin((s.g - 5150) / 745 * 2 * np.pi)
        for _ in range(120):
            s.feed(s.row() + ripple)
        self.assertEqual(s.clf.findings(s.t), [])

    def test_noise_only(self):
        s = Sim("5", 5150, 5895)
        s.quiet(200)
        self.assertEqual(s.clf.findings(s.t), [])

    def test_grid_change_resets(self):
        s = Sim("2.4", 2400, 2495)
        s.quiet()
        s.clf.feed(grid(2400, 2450), s.row(), s.t)
        self.assertEqual(s.clf.row_index, 1)


if __name__ == "__main__":
    unittest.main()
