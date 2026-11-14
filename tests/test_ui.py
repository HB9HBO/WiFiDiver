import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtWidgets import QApplication
except ImportError:      # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PyQt6 fehlt")
class WaterfallItemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_ring_buffer_newest_row_on_top(self):
        from wifidiver.ui.band_panel import WaterfallItem
        w = WaterfallItem()
        w.set_lut(np.stack([np.arange(256)] * 3, 1).astype(np.uint8))
        w.lo, w.hi = 0, 255
        rows = 4
        w.alloc(rows, 3)
        w.set_rect(QRectF(0, 0, 3, rows))
        wf = np.zeros((rows, 3))
        pos = 0
        for v in (10, 20, 30, 40, 50, 60):          # Ring läuft über
            pos = (pos - 1) % rows
            wf[pos] = v
            w.put_row(pos, wf[pos])
        img = QImage(3, rows, QImage.Format.Format_RGB32)
        img.fill(0)
        p = QPainter(img)
        w.paint(p)
        p.end()
        self.assertEqual([img.pixelColor(1, y).red() for y in range(rows)], [60, 50, 40, 30])

    def test_panel_rows_and_age(self):
        from wifidiver.ui.band_panel import GRID_BINS, BandPanel
        p = BandPanel("2.4")
        p.set_rows(50)
        f = 2400 + (np.arange(950) + 0.5) * 0.1
        for k in range(60):
            p.push_sweeps([(f, np.full(950, -90.0 + k))], 1000.0 + k)
        p.refresh()
        newest = p.wf[p.wf_pos]
        self.assertEqual(len(newest), GRID_BINS)
        self.assertAlmostEqual(float(np.nanmax(newest)), -31.0)
        self.assertEqual(p.wf_ts[(p.wf_pos + 1) % p.rows], 1058.0)   # Zeile 1 = vorletzte


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(QApplication is None, "PyQt6 fehlt")
class RangeArrowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_step_channel_5ghz(self):
        from wifidiver.ui.sdr_panel import SdrPanel
        p = SdrPanel()
        p.set_range_limits(5150, 5895, 5150, 5895)
        p.step_channel("min", 1)
        self.assertEqual(p.range_values(), (5170.0, 5895.0))        # Kanal 36 unterer Rand
        self.assertIn("ab Kanal 36", p.lbl_min.text())
        p.step_channel("max", -1)
        self.assertEqual(p.range_values()[1], 5875.0)               # bis Kanal 173
        for _ in range(8):
            p.step_channel("min", 1)
        self.assertEqual(p.range_values()[0], 5490.0)               # Lücke 64 -> 100 übersprungen
        p.step_channel("min", -1)
        self.assertEqual(p.range_values()[0], 5310.0)               # zurück auf Kanal 64
        p.step_channel("max", -1)
        p.set_range_limits(5150, 5895, 5170, 5190)
        p.step_channel("min", 1)                                    # schiebt max mit
        self.assertEqual(p.range_values(), (5190.0, 5210.0))

    def test_step_channel_24ghz_clamps(self):
        from wifidiver.ui.sdr_panel import SdrPanel
        p = SdrPanel()
        p.set_range_limits(2400, 2495, 2402, 2494)
        p.step_channel("min", -1)
        self.assertEqual(p.range_values()[0], 2400.0)               # bis an den Bandrand
        p.step_channel("max", 1)
        self.assertEqual(p.range_values()[1], 2495.0)
        p.step_channel("max", -1)
        self.assertIn("bis Kanal 14", p.lbl_max.text())


@unittest.skipIf(QApplication is None, "PyQt6 fehlt")
class LiveAveragingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_live_averaging(self):
        from wifidiver.ui.band_panel import BandPanel
        p = BandPanel("2.4")
        p.detect_on = False
        f = np.linspace(2400, 2495, 3000)
        p.push_sweep(f, np.full(f.size, -90.0), 0.0)
        p.push_sweep(f, np.full(f.size, -50.0), 0.04)
        self.assertAlmostEqual(float(np.nanmean(p.live)), -50.0, places=3)   # 0 % = ungeglättet
        p.set_live_averaging(100)
        p.push_sweep(f, np.full(f.size, -90.0), 0.08)
        m = float(np.nanmean(p.live))
        self.assertTrue(-55.0 < m < -52.0, m)                                # nur langsam nachgeführt
        for i in range(200):
            p.push_sweep(f, np.full(f.size, -90.0), 0.12 + i * 0.04)
        self.assertAlmostEqual(float(np.nanmean(p.live)), -90.0, places=1)


@unittest.skipIf(QApplication is None, "PyQt6 fehlt")
class MonitorPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_nets_and_events_tables(self):
        import tempfile
        from PyQt6.QtCore import QSettings
        from tests.test_monitor import AP, BCAST, R_CHANNEL, R_FRAME, R_READY, beacon, deauth, radiotap, rec
        from wifidiver.ui.monitor_page import MonitorPage
        from wifidiver.wifi.monitor import MonitorStats
        st = MonitorStats()
        st.feed(rec(R_READY, b"wdmon0", origlen=1) + rec(R_CHANNEL, aux=250))
        for i in range(10):
            st.feed(rec(R_FRAME, radiotap(-30) + beacon(seq=100 + i), ts=i * 100_000_000))
        for i in range(15):
            st.feed(rec(R_FRAME, radiotap(-75) + deauth(dst=BCAST, seq=7), ts=(1000 + i * 20) * 1_000_000))
        snap = st.snapshot()
        with tempfile.TemporaryDirectory() as d:
            page = MonitorPage(QSettings(os.path.join(d, "t.conf"), QSettings.Format.IniFormat))
            alerts = []
            page.alert.connect(alerts.append)
            page.backend.snapshot = snap
            page.backend.poll = lambda: True
            page._poll()
            self.assertEqual(alerts, [1])
            self.assertIn("1 auffällig", page.sub.tabText(2))
            page.sub.setCurrentIndex(1)
            page._show()
            self.assertEqual(page.net_table.rowCount(), 1)
            col = MonitorPage.NET_COLS.index("Deauth/Disassoc")
            self.assertEqual(page.net_table.item(0, col).text(), "15")
            page.sub.setCurrentIndex(2)
            page._show()
            self.assertEqual(page.ev_table.rowCount(), 1)
            self.assertIn("Angriff", page.ev_table.item(0, 1).text())
            self.assertIn("alle", page.ev_table.item(0, 5).text())
            self.assertIn("-75 dBm", page.meter_lbl.text())


@unittest.skipIf(QApplication is None, "PyQt6 fehlt")
class AutoLevelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_partly_filled_ring_buffer(self):
        from wifidiver.ui.band_panel import BandPanel
        p = BandPanel("2.4")
        p.reset_sdr()
        grid = p.grid
        for _ in range(12):                       # weit weniger Zeilen als der Puffer fasst
            p.push_sweep(grid, np.full(grid.size, -90.0), 0.0)
        lv = p.auto_levels()
        self.assertIsNotNone(lv)
        self.assertAlmostEqual(lv[0], -93.0, places=3)
