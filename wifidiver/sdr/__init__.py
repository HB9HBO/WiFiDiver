from .demo import DemoBackend
from .e310 import E310Backend
from .hackrf import HackRFBackend
from .uhd_sdr import UHDBackend

BACKENDS = {"HackRF": HackRFBackend, "UHD": UHDBackend, "E310": E310Backend, "Demo": DemoBackend}

__all__ = ["BACKENDS", "DemoBackend", "E310Backend", "HackRFBackend", "UHDBackend"]
