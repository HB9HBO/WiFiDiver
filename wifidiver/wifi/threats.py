"""Erkennung verdächtiger Frames im Monitormodus (rein passiv).

Erkannt werden:
  - Deauth/Disassoc: Flut, an alle Clients, und Hinweise auf Fälschung
      * ungeschützt, obwohl das Netz PMF (802.11w) verlangt  -> sicher gefälscht
      * Pegel passt nicht zum angeblichen Absender (Beacons des APs bzw. Frames des Clients)
      * Sequenznummer passt nicht zu den Beacons des APs
      * Sequenznummer bleibt gleich (typisch für Deauther-Werkzeuge)
  - Beacon-Flut (sehr viele neue Netze in kurzer Zeit)
  - Evil Twin (gleiche SSID wie ein verschlüsseltes Netz, aber offen/WEP)
  - Karma-AP (ein Sender antwortet auf viele verschiedene gesuchte SSIDs)

Die Absenderadresse eines gefälschten Frames ist die des Opfers – die echte Adresse des Angreifers
steht nicht im Frame. „Möglicher Sender“ ist deshalb nur ein Verdacht: Geräte, die zur selben Zeit
mit ähnlichem Pegel gehört wurden. Wirklich finden lässt sich der Sender über den Pegel (Ortung).
"""

from __future__ import annotations

from collections import deque

from .ieee80211 import is_unicast, iter_ies, mac

WINDOW_S = 10.0          # Zeitfenster für die Rate
FLOOD_N = 10             # so viele Deauths im Fenster gelten als Flut
SIG_DELTA = 10.0         # dB Abweichung vom angeblichen Absender
SUSPECT_DB = 4.0         # dB Toleranz für „möglicher Sender“
BEACON_FLOOD_N = 40      # neue Netze im Fenster
KARMA_N = 4              # verschiedene SSIDs in Probe Responses eines Senders
KEEP_S = 3600.0          # Ereignisse so lange behalten
MAX_INCIDENTS = 100

DEAUTH, DISASSOC = "Deauth", "Disassoc"
BEACON_FLOOD, EVIL_TWIN, KARMA = "Beacon-Flut", "Evil Twin?", "Karma-AP?"

REASONS = {
    1: "unbestimmt", 2: "Anmeldung ungültig", 3: "verlässt das Netz", 4: "Inaktivität",
    5: "AP überlastet", 6: "nicht authentifiziert (Klasse 2)", 7: "nicht verbunden (Klasse 3)",
    8: "verlässt das Netz", 9: "nicht authentifiziert", 14: "MIC-Fehler", 15: "4-Wege-Handshake abgelaufen",
    16: "Gruppenschlüssel abgelaufen", 23: "802.1X fehlgeschlagen", 34: "zu viele Verluste",
}


def reason_text(code: int) -> str:
    return f"{code} {REASONS[code]}" if code in REASONS else str(code)


class _Inc:
    __slots__ = ("kind", "bssid", "src", "targets", "n_targets", "count", "first", "last", "freq",
                 "reasons", "sig_sum", "sig_n", "sig_last", "ref", "sig_checked", "sig_bad",
                 "seq_checked", "seq_bad", "seqs", "pmf_bad", "protected", "times", "peak", "note")

    def __init__(self, kind, bssid, src, now, freq):
        self.kind, self.bssid, self.src = kind, bssid, src
        self.targets: set[str] = set()
        self.n_targets = 0
        self.count = 0
        self.first = self.last = now
        self.freq = freq
        self.reasons: set[int] = set()
        self.sig_sum = 0.0
        self.sig_n = 0
        self.sig_last: float | None = None
        self.ref: float | None = None
        self.sig_checked = self.sig_bad = 0
        self.seq_checked = self.seq_bad = 0
        self.seqs: set[int] = set()
        self.pmf_bad = 0
        self.protected = 0
        self.times: deque = deque()
        self.peak = 0
        self.note = ""

    def hit(self, now: float) -> None:
        self.count += 1
        self.last = now
        t = self.times
        t.append(now)
        while t and now - t[0] > WINDOW_S:
            t.popleft()
        self.peak = max(self.peak, len(t))


class ThreatDetector:
    """Wird von MonitorStats mit den einzelnen Frames gefüttert; kennt deren Netze und Clients."""

    def __init__(self, stats):
        self.stats = stats
        self.inc: dict[tuple, _Inc] = {}
        self.ap_seq: dict[str, tuple[int, float]] = {}     # BSSID -> (Sequenznummer, Zeit) aus Beacons
        self.pr_ssids: dict[str, set[str]] = {}            # Sender -> SSIDs in Probe Responses
        self.new_bss: deque = deque()
        self.last_hit: tuple | None = None                 # (Pegel, Zeit, Frequenz, BSSID) für die Ortung

    # -- Fütterung -----------------------------------------------------------------
    def beacon(self, b: bytes, o: int, bssid: str, src: str, now: float, new: bool) -> None:
        if src == bssid:
            self.ap_seq[bssid] = (((b[o + 22] | (b[o + 23] << 8)) >> 4), now)
        if new:
            nb = self.new_bss
            nb.append(now)
            while nb and now - nb[0] > WINDOW_S:
                nb.popleft()
            if len(nb) >= BEACON_FLOOD_N:
                inc = self._get(BEACON_FLOOD, "", "", now, 0)
                inc.hit(now)
                inc.peak = max(inc.peak, len(nb))
                inc.note = f"{len(nb)} neue Netze in {WINDOW_S:.0f} s"

    def probe_response(self, src: str, ssid: str | None, freq: int, now: float) -> None:
        if not ssid:
            return
        s = self.pr_ssids.get(src)
        if s is None:
            if len(self.pr_ssids) > 4000:
                self.pr_ssids.clear()
            s = self.pr_ssids[src] = set()
        if ssid in s or len(s) > 64:
            return
        s.add(ssid)
        if len(s) >= KARMA_N:
            inc = self._get(KARMA, src, src, now, freq)
            inc.hit(now)
            inc.targets = set(sorted(s)[:8])
            inc.n_targets = len(s)
            inc.note = f"antwortet auf {len(s)} verschiedene SSIDs"

    def deauth(self, b: bytes, o: int, sub: int, freq: int, sig, now: float) -> None:
        fc1 = b[o + 1]
        dst, src, bssid = mac(b, o + 4), mac(b, o + 10), mac(b, o + 16)
        seq = (b[o + 22] | (b[o + 23] << 8)) >> 4
        protected = bool(fc1 & 0x40)
        reason = None
        mme = False
        if not protected and len(b) >= o + 26:
            reason = b[o + 24] | (b[o + 25] << 8)
            mme = any(eid == 76 for eid, _ in iter_ies(b, o + 26))
        kind = DEAUTH if sub == 12 else DISASSOC
        inc = self._get(kind, bssid, src, now, freq)
        inc.hit(now)
        inc.freq = freq
        if len(inc.targets) < 256:
            inc.targets.add(dst)
        if reason is not None:
            inc.reasons.add(reason)
        if len(inc.seqs) < 64:
            inc.seqs.add(seq)
        if protected:
            inc.protected += 1
        st = self.stats
        bss = st.bss.get(bssid)
        # Pegel des angeblichen Absenders
        if src == bssid:
            ref = bss.signal if bss is not None else None
        else:
            s = st.sta.get(src)
            ref = s.signal if s is not None else None
        if sig:
            inc.sig_sum += sig
            inc.sig_n += 1
            inc.sig_last = float(sig)
            self.last_hit = (float(sig), now, freq, bssid)
            if ref is not None:
                inc.ref = ref
                inc.sig_checked += 1
                if abs(sig - ref) >= SIG_DELTA:
                    inc.sig_bad += 1
        # Sequenznummer im Vergleich zu den Beacons des APs
        if src == bssid:
            last = self.ap_seq.get(bssid)
            if last is not None and now - last[1] <= 5.0:
                inc.seq_checked += 1
                if (seq - last[0]) & 0xFFF > 1024:
                    inc.seq_bad += 1
        # PMF verlangt: Unicast muss verschlüsselt sein, Broadcast ein MME (BIP) tragen
        if bss is not None and bss.info is not None and bss.info.get("pmf") == "required" \
                and not protected and (is_unicast(dst) or not mme):
            inc.pmf_bad += 1

    def _get(self, kind, bssid, src, now, freq) -> _Inc:
        key = (kind, bssid, src)
        inc = self.inc.get(key)
        if inc is None or now - inc.last > 300:
            if len(self.inc) >= MAX_INCIDENTS:
                oldest = min(self.inc, key=lambda k: self.inc[k].last)
                del self.inc[oldest]
            inc = self.inc[key] = _Inc(kind, bssid, src, now, freq)
        return inc

    # -- Auswertung ----------------------------------------------------------------
    def _evil_twins(self, now: float) -> None:
        by_ssid: dict[str, list] = {}
        for k, v in self.stats.bss.items():
            if v.ssid and v.info is not None and now - v.last < 120:
                by_ssid.setdefault(v.ssid, []).append((k, v))
        for ssid, lst in by_ssid.items():
            if len(lst) < 2:
                continue
            strong = [k for k, v in lst if v.info["security"] not in ("Offen", "WEP")]
            if not strong:
                continue
            for k, v in lst:
                if v.info["security"] in ("Offen", "WEP"):
                    inc = self._get(EVIL_TWIN, k, k, now, int(v.info["freq"]))
                    inc.last = v.last
                    inc.count = v.beacons
                    if v.signal is not None:
                        inc.sig_last = v.signal
                        inc.sig_sum, inc.sig_n = v.signal, 1
                    inc.targets = set(strong[:8])
                    inc.n_targets = len(strong)
                    inc.note = (f"„{ssid}“ ist sonst verschlüsselt, dieser AP sendet {v.info['security']}")

    def _suspects(self, inc: _Inc) -> list[tuple[str, float]]:
        if not inc.sig_n:
            return []
        avg = inc.sig_sum / inc.sig_n
        skip = {inc.src, inc.bssid} | inc.targets
        out = []
        for d in (self.stats.sta, self.stats.bss):
            for k, v in d.items():
                if v.signal is None or k in skip or abs(v.last - inc.last) > 15:
                    continue
                diff = abs(v.signal - avg)
                if diff <= SUSPECT_DB:
                    out.append((diff, k, v.signal))
        out.sort()
        return [(k, round(s, 0)) for _, k, s in out[:3]]

    def snapshot(self, now: float) -> list[dict]:
        self._evil_twins(now)
        for k in [k for k, v in self.inc.items() if now - v.last > KEEP_S]:
            del self.inc[k]
        bss = self.stats.bss
        for k in [k for k in self.ap_seq if k not in bss]:
            del self.ap_seq[k]
        out = []
        for inc in self.inc.values():
            hints: list[str] = []
            forged = likely = False
            flood = inc.kind in (DEAUTH, DISASSOC) and inc.peak >= FLOOD_N
            if inc.kind in (DEAUTH, DISASSOC):
                if inc.pmf_bad:
                    forged = True
                    hints.append("ungeschützt, obwohl das Netz PMF verlangt – sicher gefälscht")
                if inc.sig_checked and inc.sig_bad * 2 >= inc.sig_checked:
                    likely = True
                    hints.append(f"Pegel {inc.sig_sum / inc.sig_n:.0f} dBm, angeblicher Absender "
                                 f"{inc.ref:.0f} dBm")
                if inc.seq_checked and inc.seq_bad * 2 >= inc.seq_checked:
                    # allein nicht beweiskräftig: manche APs zählen Beacons in der Hardware getrennt
                    likely = likely or inc.count >= 3
                    hints.append("Sequenznummern passen nicht zu den Beacons des APs")
                if inc.count >= 5 and len(inc.seqs) <= 2:
                    likely = True
                    hints.append("Sequenznummer bleibt gleich (typisch für Deauther)")
                if any(not is_unicast(t) for t in inc.targets):
                    hints.append("an alle Clients")
                if inc.protected and inc.protected == inc.count:
                    hints.append("geschützt (PMF) – vom echten Absender")
                if flood:
                    hints.append(f"bis {inc.peak} in {WINDOW_S:.0f} s")
                if forged:
                    verdict, sev = ("Angriff – gefälscht" if flood else "gefälscht"), 2
                elif flood:
                    verdict, sev = ("Angriff – wahrscheinlich gefälscht" if likely else "Flut"), 2
                elif likely:
                    verdict, sev = "wahrscheinlich gefälscht", 1
                elif "an alle Clients" in hints:
                    verdict, sev = "auffällig", 1
                else:
                    verdict, sev = "vermutlich normal", 0
            else:
                verdict, sev = "verdächtig", 1 if inc.kind == EVIL_TWIN else 2
                hints.append(inc.note)
            sus = self._suspects(inc) if (likely or forged) and now - inc.last < 120 else []
            out.append({
                "kind": inc.kind, "bssid": inc.bssid, "src": inc.src,
                "targets": sorted(inc.targets)[:8], "n_targets": max(inc.n_targets, len(inc.targets)),
                "count": inc.count, "first": inc.first, "last": inc.last, "freq": inc.freq,
                "reasons": [reason_text(r) for r in sorted(inc.reasons)],
                "signal": inc.sig_sum / inc.sig_n if inc.sig_n else None, "sig_last": inc.sig_last,
                "ref": inc.ref, "peak": inc.peak, "verdict": verdict, "severity": sev,
                "hints": hints, "suspects": sus,
            })
        out.sort(key=lambda d: -d["last"])
        return out
