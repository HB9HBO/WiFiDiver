"""Herstellerauflösung über die IEEE-OUI-Datenbank (Paket ieee-data)."""

from __future__ import annotations

import os

_PATHS = [
    "/usr/share/ieee-data/oui.txt",
    "/usr/share/misc/oui.txt",
    "/var/lib/ieee-data/oui.txt",
]

_db: dict[str, str] | None = None


def _load() -> dict[str, str]:
    db: dict[str, str] = {}
    for path in _PATHS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "(base 16)" in line:
                        prefix, _, name = line.partition("(base 16)")
                        db[prefix.strip().upper()] = name.strip()
        except OSError:
            continue
        if db:
            break
    return db


def lookup(bssid: str) -> str:
    global _db
    if _db is None:
        _db = _load()
    mac = bssid.replace(":", "").replace("-", "").upper()
    if len(mac) < 6:
        return ""
    try:
        first = int(mac[:2], 16)
    except ValueError:
        return ""
    name = _db.get(mac[:6], "")
    if not name and first & 0x02:
        return "(lokal verwaltet)"
    return name
