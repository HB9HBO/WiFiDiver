import random
import struct
import time
import unittest

from wifidiver.wifi.ieee80211 import airtime_us, parse_beacon_body, parse_radiotap
from wifidiver.wifi.monitor import (R_CHANNEL, R_FRAME, R_MSG, R_READY, MonitorStats, _HDR,
                                    ap_from_monitor)

AP = bytes.fromhex("aabbcc000001")
STA = bytes.fromhex("020000123456")        # lokal verwaltet = zufällige Adresse
BCAST = b"\xff" * 6


def radiotap(signal=-50, rate=12.0, flags=0x00):
    present = (1 << 1) | (1 << 2) | (1 << 5)
    body = bytes([flags, int(rate * 2), signal & 0xFF])
    return struct.pack("<BBHI", 0, 0, 8 + len(body), present) + body


def ie(eid, data):
    return bytes([eid, len(data)]) + data


def beacon(ssid=b"Testnetz", channel=6, sub=8, bssid=AP, seq=0, rsn_caps=0, open_=False):
    rsn = (b"\x01\x00" + b"\x00\x0f\xac\x04" + b"\x01\x00" + b"\x00\x0f\xac\x04"
           + b"\x02\x00" + b"\x00\x0f\xac\x02" + b"\x00\x0f\xac\x08" + struct.pack("<H", rsn_caps))
    ies = (ie(0, ssid) + ie(3, bytes([channel])) + ie(11, b"\x05\x00\x80\x00\x00")
           + ie(45, b"\x00" * 26) + ie(61, bytes([channel, 0x05]) + b"\x00" * 20)
           + (b"" if open_ else ie(48, rsn)) + ie(255, b"\x23" + b"\x00" * 20))
    body = b"\x00" * 8 + struct.pack("<HH", 100, 0x0001 if open_ else 0x0011) + ies
    hdr = struct.pack("<BBH", sub << 4, 0, 0) + BCAST + bssid + bssid + struct.pack("<H", seq << 4)
    return hdr + body


def deauth(dst=STA, src=AP, bssid=AP, seq=0, reason=7, sub=12, protected=False):
    hdr = struct.pack("<BBH", sub << 4, 0x40 if protected else 0, 0) + dst + src + bssid
    return hdr + struct.pack("<H", seq << 4) + struct.pack("<H", reason)


def data_to_ap(length=1500):
    hdr = struct.pack("<BBH", 0x88, 0x01 | 0x08, 0) + AP + STA + AP + b"\x00\x00" + b"\x00\x00"
    return hdr, length


def probe_req(ssid=b"Zuhause"):
    hdr = struct.pack("<BBH", 0x40, 0, 0) + BCAST + STA + BCAST + b"\x00\x00"
    return hdr + ie(0, ssid)


T0 = time.monotonic_ns()


def rec(typ, payload=b"", freq=2437, origlen=None, aux=0, ts=0):
    ts += T0
    return _HDR.pack(typ, 0, len(payload), freq, len(payload) if origlen is None else origlen,
                     aux, ts) + payload


class RadiotapTest(unittest.TestCase):
    def test_basic_fields(self):
        rt = parse_radiotap(radiotap(-61, 24.0))
        self.assertEqual(rt.signal, -61)
        self.assertEqual(rt.rate, 24.0)
        self.assertEqual(rt.length, 11)
        self.assertAlmostEqual(airtime_us(rt, 300), 20 + 300 * 8 / 24.0)

    def test_he_rate(self):
        present = (1 << 5) | (1 << 23)
        # Signal, dann HE (Ausrichtung 2): data1 MCS bekannt, data3 MCS 7, data5 80 MHz, data6 2 NSS
        he = struct.pack("<HHHHHH", 0x0020, 0, 7 << 8, 0, 2, 2)
        b = struct.pack("<BBHI", 0, 0, 8 + 2 + 12, present) + bytes([0xC4, 0]) + he
        rt = parse_radiotap(b)
        self.assertEqual(rt.phy, "he")
        self.assertEqual(rt.signal, -60)
        self.assertAlmostEqual(rt.rate, 86 * 2 * 4.5, places=1)

    def test_garbage_never_raises(self):
        rnd = random.Random(7)
        for _ in range(3000):
            b = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 80)))
            parse_radiotap(b)
            parse_beacon_body(b, 2437)
        st = MonitorStats()
        blob = bytearray()
        for _ in range(500):
            payload = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 120)))
            if payload and rnd.random() < 0.5:
                payload = radiotap() + payload
            blob += rec(R_FRAME, payload)
        self.assertEqual(st.feed(blob), len(blob))
        st.snapshot()


class BeaconTest(unittest.TestCase):
    def test_parse_body(self):
        b = beacon()
        info = parse_beacon_body(b[24:], 2437)
        self.assertEqual(info["ssid"], "Testnetz")
        self.assertEqual(info["freq"], 2437.0)
        self.assertEqual(info["width"], 40)
        self.assertEqual(info["ht_offset"], "above")
        self.assertEqual(info["security"], "WPA2/WPA3-Personal")
        self.assertEqual(info["gen"], "Wi-Fi 6")
        self.assertEqual(info["stations"], 5)
        self.assertAlmostEqual(info["utilization"], 128 * 100 / 255)

    def test_adjacent_channel_beacon_uses_ds_channel(self):
        info = parse_beacon_body(beacon(channel=6)[24:], 2427)   # auf Kanal 4 gehört
        self.assertEqual(info["freq"], 2437.0)


class StatsTest(unittest.TestCase):
    def feed(self, st, *recs):
        blob = b"".join(recs)
        self.assertEqual(st.feed(blob), len(blob))

    def test_aggregation(self):
        st = MonitorStats()
        t = 0
        self.feed(st, rec(R_READY, b"wdmon3", origlen=1, ts=t),
                  rec(R_CHANNEL, ts=t, aux=250))
        for i in range(10):
            self.feed(st, rec(R_FRAME, radiotap(-50 - i % 2) + beacon(), ts=t + i * 10_000_000))
        hdr, length = data_to_ap()
        self.feed(st, rec(R_FRAME, radiotap(-70, 54.0) + hdr, origlen=11 + length, ts=t + 200_000_000))
        self.feed(st, rec(R_FRAME, radiotap(-72) + probe_req(), ts=t + 210_000_000))
        self.feed(st, rec(R_MSG, "Hinweis".encode()))
        self.feed(st, rec(R_CHANNEL, freq=2462, ts=t + 250_000_000, aux=250))
        snap = st.snapshot()
        self.assertEqual(snap["ready"], ("wdmon3", "zusätzliches Monitor-Interface"))
        self.assertEqual(snap["messages"], ["Hinweis"])
        bssid = AP.hex(":")
        sta = STA.hex(":")
        b = snap["bss"][bssid]
        self.assertEqual(b["ssid"], "Testnetz")
        self.assertEqual(b["beacons"], 10)
        self.assertEqual(b["clients"], 1)
        s = snap["sta"][sta]
        self.assertEqual(s["bssid"], bssid)
        self.assertTrue(s["random"])
        self.assertEqual(s["retries"], 1)
        self.assertEqual(s["probes"], ["Zuhause"])
        c = snap["chan"][2437]
        self.assertGreater(c["busy"], 0)
        self.assertLess(c["busy"], 100)
        self.assertAlmostEqual(c["fps"], 12 / 0.25)
        self.assertTrue(snap["samples"])
        ap = ap_from_monitor(bssid, b)
        self.assertEqual(ap.ssid, "Testnetz")
        self.assertEqual(ap.center_freq, 2447.0)
        self.assertEqual(ap.extra["source"], "monitor")
        self.assertLess(ap.signal, -49)

    def test_partial_record_is_kept(self):
        st = MonitorStats()
        r = rec(R_FRAME, radiotap() + beacon())
        self.assertEqual(st.feed(r[:-5]), 0)
        self.assertEqual(st.feed(r), len(r))

    def test_hidden_ssid_from_probe_response(self):
        st = MonitorStats()
        self.feed(st, rec(R_FRAME, radiotap() + beacon(ssid=b"\x00" * 6)))
        self.feed(st, rec(R_FRAME, radiotap() + beacon(ssid=b"Geheim", sub=5), ts=1))
        b = st.snapshot()["bss"][AP.hex(":")]
        self.assertTrue(b["hidden"])
        self.assertEqual(b["ssid"], "Geheim")


class ThreatTest(unittest.TestCase):
    MS = 1_000_000

    def setUp(self):
        self.st = MonitorStats()
        self.st.feed(rec(R_READY, b"wdmon0", origlen=1) + rec(R_CHANNEL, aux=250))

    def frame(self, payload, sig=-30, ts=0):
        self.st.feed(rec(R_FRAME, radiotap(sig) + payload, ts=ts))

    def beacons(self, n=10, **kw):
        for i in range(n):
            self.frame(beacon(seq=100 + i, **kw), sig=-30, ts=i * 100 * self.MS)

    def events(self, kind=None):
        ev = self.st.snapshot()["events"]
        return [e for e in ev if kind is None or e["kind"] == kind]

    def test_forged_deauth_flood(self):
        self.beacons()
        for i in range(20):        # Deauther: schwächer, Sequenznummer konstant, an alle
            self.frame(deauth(dst=BCAST, seq=7), sig=-75, ts=(1000 + i * 20) * self.MS)
        e = self.events("Deauth")[0]
        self.assertEqual(e["count"], 20)
        self.assertEqual(e["severity"], 2)
        self.assertIn("Angriff", e["verdict"])
        self.assertIn("gefälscht", e["verdict"])
        hints = " ".join(e["hints"])
        self.assertIn("Pegel -75 dBm", hints)
        self.assertIn("Sequenznummer bleibt gleich", hints)
        self.assertIn("an alle", hints)
        self.assertEqual(e["reasons"], ["7 nicht verbunden (Klasse 3)"])
        self.assertEqual(self.st.snapshot()["bss"][AP.hex(":")]["deauth"], 20)

    def test_pmf_violation_is_forged(self):
        self.beacons(rsn_caps=0xC0)            # PMF erforderlich
        self.assertEqual(self.st.bss[AP.hex(":")].info["pmf"], "required")
        self.frame(deauth(seq=110), sig=-30, ts=1100 * self.MS)
        e = self.events("Deauth")[0]
        self.assertEqual(e["verdict"], "gefälscht")
        self.assertEqual(e["severity"], 2)

    def test_single_genuine_deauth_is_normal(self):
        self.beacons()
        self.frame(deauth(seq=110, reason=3), sig=-31, ts=1100 * self.MS)
        e = self.events("Deauth")[0]
        self.assertEqual(e["severity"], 0)
        self.assertEqual(e["verdict"], "vermutlich normal")

    def test_protected_deauth(self):
        self.beacons(rsn_caps=0xC0)
        self.frame(deauth(seq=110, protected=True), sig=-30, ts=1100 * self.MS)
        e = self.events("Deauth")[0]
        self.assertEqual(e["severity"], 0)
        self.assertEqual(e["reasons"], [])
        self.assertIn("geschützt (PMF) – vom echten Absender", e["hints"])

    def test_suspect_by_signal(self):
        self.beacons()
        other = bytes.fromhex("001122334455")
        hdr = struct.pack("<BBH", 0x88, 0x01, 0) + AP + other + AP + b"\x00\x00" + b"\x00\x00"
        self.frame(hdr, sig=-74, ts=900 * self.MS)            # unbekannter Client mit ähnlichem Pegel
        for i in range(12):
            self.frame(deauth(seq=7), sig=-75, ts=(1000 + i * 10) * self.MS)
        e = self.events("Deauth")[0]
        self.assertEqual(e["suspects"][0][0], other.hex(":"))

    def test_evil_twin(self):
        self.beacons()
        twin = bytes.fromhex("aabbcc000099")
        self.frame(beacon(bssid=twin, open_=True), ts=1100 * self.MS)
        e = self.events("Evil Twin?")
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["bssid"], twin.hex(":"))

    def test_karma(self):
        for i, name in enumerate((b"Zuhause", b"Buero", b"Hotel", b"Flughafen")):
            self.frame(beacon(ssid=name, sub=5), ts=i * 50 * self.MS)
        e = self.events("Karma-AP?")
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["n_targets"], 4)

    def test_beacon_flood(self):
        for i in range(50):
            self.frame(beacon(ssid=b"x%d" % i, bssid=bytes([0x02, 0, 0, 0, 0, i])), ts=i * 10 * self.MS)
        self.assertEqual(len(self.events("Beacon-Flut")), 1)


if __name__ == "__main__":
    unittest.main()
