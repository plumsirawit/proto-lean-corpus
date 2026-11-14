"""Interrupt Python polling without losing shutdown signals to EINTR handling."""

import signal


def install_shutdown_handler():
    def interrupted(signum, frame):
        # selectors.select() catches InterruptedError and returns normally.
        # KeyboardInterrupt propagates through polling to the cleanup handlers.
        raise KeyboardInterrupt(f"processing interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
