import struct
import unittest

import numpy as np

from wifidiver.sdr.base import GridMapper, resample_to_grid
from wifidiver.sdr.hackrf import SweepParser

NB = 51          # Bins pro Datensatz (5 MHz bei ~98 kHz)


def record(lo_mhz: int, values: np.ndarray) -> bytes:
    lo, hi = lo_mhz * 1_000_000, (lo_mhz + 5) * 1_000_000
    return struct.pack("<IQQ", 16 + 4 * len(values), lo, hi) + values.astype("<f4").tobytes()


def sweep_bytes(start: int, stop: int, level: float) -> bytes:
    """Datensätze in der verschränkten Reihenfolge von hackrf_sweep (2400, 2410, 2405, 2415, …)."""
    out = b""
    for f in range(start, stop, 20):
        for lo in (f, f + 10, f + 5, f + 15):
            out += record(lo, np.full(NB, level + (lo - start) / 100.0))
    return out


class SweepParserTest(unittest.TestCase):
    def test_interleaved_records_and_odd_chunks(self):
        data = b"".join(sweep_bytes(2400, 2500, -90.0 + k) for k in range(4))
        p = SweepParser()
        sweeps = []
        for i in range(0, len(data), 333):                 # beliebige Pipe-Stückelung
            sweeps += p.feed(data[i:i + 333])
        self.assertEqual(len(sweeps), 3)                   # der letzte endet erst mit dem nächsten
        for k, (f, pw) in enumerate(sweeps):
            self.assertEqual(len(f), 20 * NB)
            self.assertTrue(np.all(np.diff(f) > 0))        # nach Frequenz sortiert
            self.assertAlmostEqual(f[0], 2400 + 5 / NB / 2, places=6)
            self.assertAlmostEqual(f[-1], 2500 - 5 / NB / 2, places=6)
            self.assertAlmostEqual(float(pw[0]), -90.0 + k, places=4)
            self.assertAlmostEqual(float(pw[-1]), -90.0 + k + 0.95, places=4)
        self.assertIs(sweeps[0][0], sweeps[1][0])          # Frequenzachse wird wiederverwendet

    def test_bad_format(self):
        with self.assertRaises(ValueError):
            SweepParser().feed(struct.pack("<I", 7) + b"\0" * 64)


class GridMapperTest(unittest.TestCase):
    def test_matches_resample_for_single_sweep(self):
        rng = np.random.default_rng(1)
        for lo, hi, n, f0, f1 in [(2400, 2500, 1020, 2400, 2495), (5150, 5900, 7752, 5150, 5895),
                                  (2400, 2495, 4000, 2440, 2460)]:
            f = lo + (np.arange(n) + 0.5) * (hi - lo) / n
            p = rng.normal(-90, 5, n).astype(np.float32)
            ref = resample_to_grid(f, p, f0, f1, 1500)
            out = GridMapper().map([(f, p)], f0, f1, 1500)
            np.testing.assert_array_equal(np.isnan(ref), np.isnan(out))
            m = ~np.isnan(ref)
            np.testing.assert_allclose(out[m], ref[m])

    def test_multiple_sweeps_take_maximum(self):
        f = 2400 + (np.arange(3000) + 0.5) * 95 / 3000
        a = np.full(3000, -90.0)
        b = np.full(3000, -90.0)
        b[1000] = -40.0
        out = GridMapper().map([(f, a), (f, b)], 2400, 2495, 1500)
        self.assertEqual(out.max(), -40.0)
        self.assertEqual(np.argmax(out), 500)

    def test_unsorted_and_nonfinite_fall_back(self):
        f = np.array([2450.0, 2410.0, 2430.0])
        p = np.array([-50.0, -60.0, -np.inf])
        out = GridMapper().map([(f, p)], 2400, 2500, 100)
        ref = resample_to_grid(f, p, 2400, 2500, 100)
        np.testing.assert_array_equal(np.isnan(out), np.isnan(ref))


if __name__ == "__main__":
    unittest.main()
