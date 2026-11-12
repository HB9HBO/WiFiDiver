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
