import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def test_term_interrupts_active_io_poll(tmp_path):
    ready = tmp_path / "ready"
    # Reproduce the actual worker wait boundary: InterruptedError from a signal
    # handler is swallowed by selectors, so only a propagating exception works.
    program = """
import os, selectors, sys
from pathlib import Path
from lean_corpus.shutdown import install_shutdown_handler
install_shutdown_handler()
reader, writer = os.pipe()
with selectors.DefaultSelector() as poller:
    poller.register(reader, selectors.EVENT_READ)
    try:
        Path(sys.argv[1]).touch()
        poller.select(30)
    except KeyboardInterrupt:
        Path(sys.argv[1]).with_suffix('.interrupted').touch()
    else:
        raise AssertionError('shutdown was swallowed by I/O polling')
"""
    child = subprocess.Popen([sys.executable, "-c", program, str(ready)])
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert ready.exists(), "signal test did not initialize"
        os.kill(child.pid, signal.SIGTERM)
        assert child.wait(timeout=5) == 0
        assert Path(ready).with_suffix(".interrupted").exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
