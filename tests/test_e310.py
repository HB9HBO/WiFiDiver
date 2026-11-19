import socket
import struct
import threading
import time
import unittest

import numpy as np

from wifidiver.sdr.e310 import (NO_DATA, T_ERROR, T_INFO, T_SWEEP, E310Backend, FrameParser,
                                SweepDecoder, parse_info)


def frame(typ: int, payload: bytes) -> bytes:
    return struct.pack("<4sBBHI", b"E31D", typ, 1, 0, len(payload)) + payload


def sweep_payload(f0_hz: float, df_hz: float, values_db, seq: int = 1) -> bytes:
    raw = np.array([NO_DATA if np.isnan(v) else round(v * 100) for v in values_db], "<i2")
    return struct.pack("<IIddff", seq, len(raw), f0_hz, df_hz, 0.03, 50.0) + raw.tobytes()


class FrameParserTest(unittest.TestCase):
    def test_split_chunks(self):
        data = (frame(T_INFO, b"fmin_mhz=70\nfmax_mhz=6000\n")
                + frame(T_SWEEP, sweep_payload(2400.015e6, 30e3, [-70.0, -60.5, np.nan]))
                + frame(T_ERROR, "ungültig".encode()))
        p = FrameParser()
        out = []
        for i in range(0, len(data), 7):          # ungünstige Stückelung
            out += p.feed(data[i:i + 7])
        self.assertEqual([t for t, _ in out], [T_INFO, T_SWEEP, T_ERROR])
        self.assertEqual(parse_info(out[0][1]), {"fmin_mhz": "70", "fmax_mhz": "6000"})

        f, pw, dur, gain = SweepDecoder().decode(out[1][1])
        np.testing.assert_allclose(f, [2400.015, 2400.045, 2400.075])
        self.assertAlmostEqual(pw[0], -70.0, places=4)
        self.assertAlmostEqual(pw[1], -60.5, places=4)
        self.assertTrue(np.isnan(pw[2]))
        self.assertEqual(gain, 50.0)

    def test_out_of_sync(self):
        with self.assertRaises(ValueError):
            FrameParser().feed(b"XXXX" + b"\0" * 20)


class FakeServer:
    """Nimmt eine Verbindung an, protokolliert die Befehlszeilen und schickt Sweeps."""

    def __init__(self):
        self.ls = socket.socket()
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(1)
        self.port = self.ls.getsockname()[1]
        self.lines: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        c, _ = self.ls.accept()
        c.sendall(frame(T_INFO, b"fmin_mhz=70\nfmax_mhz=6000\n"))
        buf = b""
        c.settimeout(0.05)
        started = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                d = c.recv(4096)
                if not d:
                    break
                buf += d
            except socket.timeout:
                pass
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.lines.append(line.decode())
                if line.startswith(b"start"):
                    started = True
                    # Sweep eines anderen Bereichs (darf nicht ankommen), dann zwei passende
                    c.sendall(frame(T_SWEEP, sweep_payload(5150e6, 1e6, [-50.0] * 10)))
                    for k in range(2):
                        c.sendall(frame(T_SWEEP, sweep_payload(2400.5e6, 1e6, [-80.0 + k] * 100, k)))
                    c.sendall(frame(T_ERROR, b"Testfehler"))
                if line == b"stop":
                    c.close()
                    return
            if not started:
                continue
        c.close()


class BackendTest(unittest.TestCase):
    def test_roundtrip_with_fake_server(self):
        srv = FakeServer()
        b = E310Backend({"host": "127.0.0.1", "port": srv.port})
        b.start(2400, 2500)
        sweeps = []
        deadline = time.time() + 3
        while len(sweeps) < 2 and time.time() < deadline:
            try:
                sweeps.append(b.sweeps.get(timeout=0.2))
            except Exception:  # noqa: BLE001 - queue.Empty
                pass
        b.update_setting("gain", 33)
        time.sleep(0.3)
        b.close()
        srv.thread.join(timeout=2)

        self.assertEqual(len(sweeps), 2)                  # 5-GHz-Sweep verworfen
        self.assertEqual(len(sweeps[0].freqs_mhz), 100)
        self.assertAlmostEqual(sweeps[0].freqs_mhz[0], 2400.5)
        self.assertAlmostEqual(float(sweeps[1].power_db[0]), -79.0, places=4)
        self.assertEqual((sweeps[0].f_lo, sweeps[0].f_hi), (2400, 2500))

        setl = next(line for line in srv.lines if line.startswith("set "))
        for kv in ("rate=30.72", "fft=1024", "avg=16", "gain=50", "settle=100", "max_bins=4096"):
            self.assertIn(kv, setl.split())
        self.assertIn("start 2400.000000 2500.000000", srv.lines)
        self.assertIn("set gain=33", srv.lines)
        self.assertEqual(srv.lines[-1], "stop")
        self.assertIn("E310: Testfehler", [b.errors.get_nowait() for _ in range(b.errors.qsize())])

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                     # Port sicher geschlossen
        b = E310Backend({"host": "127.0.0.1", "port": port})
        b.start(2400, 2500)
        msg = b.errors.get(timeout=5)
        b.close()
        self.assertIn("nicht erreichbar", msg)


if __name__ == "__main__":
    unittest.main()
