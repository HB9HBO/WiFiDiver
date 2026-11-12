import unittest

from wifidiver.wifi.channels import BANDS
from wifidiver.wifi.overlays import (OVERLAYS, OVERLAY_BY_KEY, ble_channel_freq, describe,
                                     zigbee_channel_freq)


class OverlayTest(unittest.TestCase):
    def test_ble_channels(self):
        self.assertEqual(ble_channel_freq(37), 2402)
        self.assertEqual(ble_channel_freq(38), 2426)
        self.assertEqual(ble_channel_freq(39), 2480)
        self.assertEqual(ble_channel_freq(0), 2404)
        self.assertEqual(ble_channel_freq(10), 2424)
        self.assertEqual(ble_channel_freq(11), 2428)
        self.assertEqual(ble_channel_freq(36), 2478)
        # alle 40 Kanäle im 2-MHz-Raster 2402..2480, keine Doppelten
        self.assertEqual(sorted(ble_channel_freq(c) for c in range(40)), [2402 + 2 * k for k in range(40)])

    def test_zigbee_channels(self):
        self.assertEqual(zigbee_channel_freq(11), 2405)
        self.assertEqual(zigbee_channel_freq(26), 2480)
        with self.assertRaises(ValueError):
            zigbee_channel_freq(10)

    def test_bt_classic(self):
        ch = OVERLAY_BY_KEY["bt_classic"].channels
        self.assertEqual(len(ch), 79)
        self.assertEqual((ch[0].center, ch[-1].center), (2402, 2480))

    def test_overlays_inside_band(self):
        for o in OVERLAYS:
            b = BANDS[o.band]
            self.assertTrue(o.lo < b.f_max and o.hi > b.f_min, o.key)

    def test_describe(self):
        on = {o.key for o in OVERLAYS}
        self.assertIn("BLE Adv. 38", describe(2426, "2.4", on))
        self.assertIn("802.15.4 15", describe(2425, "2.4", on))
        self.assertIn("BT 0", describe(2402, "2.4", on))
        self.assertIn("Wetterradar", describe(5620, "5", on))
        self.assertIn("UWB 5", describe(6500, "6", on))
        self.assertEqual(describe(2426, "2.4", set()), [])


if __name__ == "__main__":
    unittest.main()
