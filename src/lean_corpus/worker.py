"""Bounded isolated sequential Lean worker with a fresh command state per request.

Only the pinned imported environment is reused. Explicit metaprogramming forms
fall back to the original per-process checker. No worker runs outside isolation.
"""

import atexit
import json
import os
from pathlib import Path
import resource
import selectors
import select
import signal
import subprocess
import sys
import tempfile
import time
from .common import digest
from .lean import (
    HELPER,
    LeanFailure,
    decode_result,
    execution_config,
    implementation_hash,
)

MAX_OUTPUT = 16 * 1024**2
_WORKERS = {}
STATS = {
    "starts": 0,
    "requests": 0,
    "header_restarts": 0,
    "fallbacks": 0,
    "peak_rss_kib": 0,
}


class Worker:
    def __init__(self, environment, runtime, memory_mb):
        self.environment, self.runtime = environment, runtime
        self.tmp = tempfile.TemporaryDirectory(prefix="corpus-worker-")
        self.root = Path(self.tmp.name)
        self.process = None
        self.stderr = None
        self.selector = selectors.DefaultSelector()
        self.buffer = b""
        self.count = 0
        self.max_cases = int(runtime.get("worker_max_cases", 512))
        self.next_id = 0
        self.tools, project, env = execution_config(environment, runtime)
        spec = {
            "reads": [
                "/usr",
                "/lib",
                "/lib64",
                "/bin",
                "/etc/ld.so.cache",
                "/dev/null",
                "/dev/urandom",
                "/dev/random",
                str(self.tools),
                str(HELPER),
                str(project),
            ],
            "write": self.tmp.name,
            "cwd": str(project),
            "command": [
                str(self.tools / "bin/lean"),
                "-j1",
                "--run",
                str(HELPER),
                "corpus-worker",
            ],
            "env": env,
            "worker": True,
        }
        command = [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--mount",
            "--pid",
            "--fork",
            sys.executable,
            "-I",
            str(Path(__file__).with_name("isolation.py")),
            json.dumps(spec),
        ]

        def limits():
            os.setsid()
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024**2,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (3600, 3601))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))

        try:
            self.stderr = (self.root / "stderr").open("w+b")
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                cwd=project,
                env=env,
                preexec_fn=limits,
                bufsize=0,
            )
            os.set_blocking(self.process.stdin.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            event, log = self.read_message(time.monotonic() + 120)
            if event.get("event") != "ready":
                raise LeanFailure("inspection_protocol_failure", log)
        except BaseException:
            self.close()
            raise
        STATS["starts"] += 1

    def diagnostics(self):
        if self.stderr is None:
            return ""
        self.stderr.flush()
        self.stderr.seek(0)
        return self.stderr.read(MAX_OUTPUT).decode(errors="replace")

    def read_message(self, deadline):
        captured = bytearray()
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                captured.extend(line + b"\n")
                if len(captured) > MAX_OUTPUT:
                    raise LeanFailure(
                        "output_limit", "checker exceeded per-request output limit"
                    )
                if line.startswith(b"CORPUS_WORKER:"):
                    try:
                        return json.loads(
                            line[len(b"CORPUS_WORKER:") :]
                        ), captured.decode(errors="replace")
                    except ValueError as error:
                        raise LeanFailure(
                            "inspection_protocol_failure", str(error)
                        ) from error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeanFailure("timeout")
            if not self.selector.select(timeout=remaining):
                raise LeanFailure("timeout")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                self.process.wait(timeout=3)
                log = captured.decode(errors="replace") + self.diagnostics()
                reason = (
                    "resource_exhaustion"
                    if self.process.returncode < 0
                    else "checker_startup_or_process_failure"
                )
                if "CORPUS_SANDBOX_ERROR" in log or "unshare:" in log:
                    reason = "isolation_unavailable"
                raise LeanFailure(reason, log)
            self.buffer += chunk
            if len(self.buffer) + len(captured) > MAX_OUTPUT:
                raise LeanFailure(
                    "output_limit", "checker exceeded per-request output limit"
                )

    def inspect(self, source, mode, target, timeout):
        self.next_id += 1
        request_id = self.next_id
        payload = (
            json.dumps(
                {"id": request_id, "source": source, "mode": mode, "target": target},
                ensure_ascii=False,
            ).encode()
            + b"\n"
        )
        deadline = time.monotonic() + timeout
        sent = 0
        try:
            while sent < len(payload):
                remaining = deadline - time.monotonic()
                if (
                    remaining <= 0
                    or not select.select(
                        [], [self.process.stdin.fileno()], [], remaining
                    )[1]
                ):
                    raise LeanFailure("timeout")
                try:
                    sent += os.write(
                        self.process.stdin.fileno(), payload[sent : sent + 65536]
                    )
                except BlockingIOError:
                    continue
        except (OSError, BrokenPipeError) as error:
            raise LeanFailure("checker_process_failure", str(error)) from error
        logs = []
        while True:
            event, log = self.read_message(deadline)
            logs.append(log)
            if event.get("id") != request_id:
                raise LeanFailure(
                    "inspection_protocol_failure",
                    "worker returned an unexpected request id",
                )
            kind = event.get("event")
            if kind == "started":
                continue
            if kind in {"restart", "fallback"}:
                return kind, event.get("message", "")
            if kind == "error":
                raise LeanFailure("checker_error", event.get("message", ""))
            if kind != "result":
                raise LeanFailure(
                    "inspection_protocol_failure", "unexpected worker event"
                )
            STATS["peak_rss_kib"] = max(STATS["peak_rss_kib"], event.get("rss_kib", 0))
            self.count += 1
            STATS["requests"] += 1
            return "result", decode_result(event["result"], mode, "".join(logs))

    def close(self):
        if self.process is not None:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=5)
            if self.process.stdout is not None:
                self.process.stdout.close()
        self.selector.close()
        if self.stderr is not None:
            self.stderr.close()
        self.tmp.cleanup()


def close_all():
    for worker in list(_WORKERS.values()):
        worker.close()
    _WORKERS.clear()


atexit.register(close_all)


def inspect_with_worker(source, environment, runtime, mode, target, timeout, memory_mb):
    tools, project, _ = execution_config(environment, runtime)
    key = digest(
        {
            "environment": environment,
            "project": str(project),
            "toolchain_dir": str(tools),
            "paths": runtime.get("lean_path", []),
            "memory_mb": memory_mb,
            "max_cases": runtime.get("worker_max_cases", 512),
            "helper": implementation_hash(),
        }
    )
    for attempt in range(2):
        worker = _WORKERS.get(key)
        if worker is not None and worker.count >= worker.max_cases:
            worker.close()
            del _WORKERS[key]
            worker = None
        if worker is None:
            if len(_WORKERS) >= 2:
                _, oldest = _WORKERS.popitem()
                oldest.close()
            worker = Worker(environment, runtime, memory_mb)
            _WORKERS[key] = worker
        try:
            kind, result = worker.inspect(source, mode, target, timeout)
        except LeanFailure:
            worker.close()
            _WORKERS.pop(key, None)
            raise
        if kind == "result":
            return result
        worker.close()
        _WORKERS.pop(key, None)
        if kind == "restart":
            STATS["header_restarts"] += 1
            if attempt == 0:
                continue
            raise LeanFailure(
                "inspection_protocol_failure", "repeated import-header restart"
            )
        STATS["fallbacks"] += 1
        from .lean import inspect

        return inspect(
            source,
            environment,
            {**runtime, "runner": "process"},
            mode,
            target,
            timeout,
            memory_mb,
        )
    raise LeanFailure("checker_process_failure")
