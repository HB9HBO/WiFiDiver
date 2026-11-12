from .demo import DemoBackend
from .hackrf import HackRFBackend
from .uhd_sdr import UHDBackend

BACKENDS = {"HackRF": HackRFBackend, "UHD": UHDBackend, "Demo": DemoBackend}

__all__ = ["BACKENDS", "DemoBackend", "HackRFBackend", "UHDBackend"]
