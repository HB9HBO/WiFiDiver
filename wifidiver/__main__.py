"""Start: python3 -m wifidiver [--source demo] [--sdr HackRF|UHD|Demo] [--start]"""

from __future__ import annotations

import argparse
import signal
import sys


def _dark_palette(app) -> None:
    from PyQt6.QtGui import QColor, QPalette
    pal = QPalette()
    base, alt, text = QColor(30, 31, 34), QColor(40, 42, 46), QColor(225, 225, 225)
    for role, col in (
        (QPalette.ColorRole.Window, QColor(37, 38, 42)),
        (QPalette.ColorRole.WindowText, text),
        (QPalette.ColorRole.Base, base),
        (QPalette.ColorRole.AlternateBase, alt),
        (QPalette.ColorRole.ToolTipBase, alt),
        (QPalette.ColorRole.ToolTipText, text),
        (QPalette.ColorRole.Text, text),
        (QPalette.ColorRole.Button, alt),
        (QPalette.ColorRole.ButtonText, text),
        (QPalette.ColorRole.Highlight, QColor(60, 110, 180)),
        (QPalette.ColorRole.HighlightedText, QColor(255, 255, 255)),
        (QPalette.ColorRole.PlaceholderText, QColor(130, 130, 130)),
    ):
        pal.setColor(role, col)
    app.setPalette(pal)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wifidiver", description="WLAN-Analyzer mit SDR-Wasserfall")
    ap.add_argument("--source", choices=["auto", "iw", "nmcli", "demo"], default="auto",
                    help="WLAN-Datenquelle (Standard: auto)")
    ap.add_argument("--iface", help="WLAN-Interface, z.B. wlan0")
    ap.add_argument("--sdr", choices=["HackRF", "UHD", "Demo"], help="SDR-Backend")
    ap.add_argument("--start", action="store_true", help="SDR-Sweep sofort starten")
    ap.add_argument("--band", choices=["2.4", "5", "6"], help="Start-Band")
    ap.add_argument("--light", action="store_true", help="helles Farbschema")
    args = ap.parse_args(argv)

    import pyqtgraph as pg
    from PyQt6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("WifiDiver")
    app.setStyle("Fusion")
    if args.light:
        pg.setConfigOptions(background="w", foreground="k", antialias=True)
    else:
        _dark_palette(app)
        pg.setConfigOptions(background=(24, 25, 28), foreground=(200, 200, 200), antialias=True)

    win = MainWindow(source=args.source, iface=args.iface, sdr=args.sdr,
                     autostart=args.start, band=args.band)
    win.show()
    signal.signal(signal.SIGINT, lambda *_: win.close())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
