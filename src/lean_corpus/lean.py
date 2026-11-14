"""Lean's own parser and elaborator, under bounded, isolated Linux execution."""

import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from .common import file_hash

HELPER = Path(__file__).with_name("lean") / "Inspect.lean"
POLICY = {
    "version": "1",
    "allowed_axioms": ["propext", "Classical.choice", "Quot.sound"],
}


class LeanFailure(Exception):
    def __init__(self, reason, detail=""):
        self.reason, self.detail = reason, detail
        super().__init__(reason + (": " + detail[:300] if detail else ""))


def implementation_hash():
    return file_hash(HELPER)


def execution_config(environment, runtime):
    toolchain = environment.get("toolchain") or "leanprover/lean4:v4.19.0"
    # Resolve only installed toolchains. Never invoke elan's implicit downloader.
    tools = (
        Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan")))
        / "toolchains"
        / toolchain.replace("/", "--").replace(":", "---")
    )
    lean = tools / "bin" / "lean"
    if not lean.is_file():
        raise LeanFailure("toolchain_unavailable", toolchain)
    project = Path(runtime.get("project_dir", str(HELPER.parent))).resolve()
    if not project.is_dir():
        raise LeanFailure("project_unavailable")
    paths = [str(project / p) for p in runtime.get("lean_path", [])]
    # Resolve dependencies from the user's prepared environment, never lake update.
    env = {
        "PATH": str(tools / "bin") + ":/usr/bin:/bin",
        "LEAN_PATH": os.pathsep.join(paths),
        "LEAN_SYSROOT": str(tools),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
    }
    return tools, project, env


def decode_result(result, mode, log=""):
    if not isinstance(result, dict) or not all(
        key in result for key in ("targets", "errors", "admissions", "imports_resolved")
    ):
        raise LeanFailure("inspection_protocol_failure", "incomplete checker result")
    diagnostics = "\n".join(result.get("errors", [])).lower()
    if any(
        marker in diagnostics
        for marker in (
            "maximum recursion depth",
            "maximum number of heartbeats",
            "deterministic timeout",
            "out of memory",
            "stack overflow",
        )
    ):
        raise LeanFailure("resource_exhaustion", log or diagnostics)
    if any(
        marker in diagnostics
        for marker in (
            "no such file or directory",
            "unknown module prefix",
            "failed to execute",
            "error executing external",
            "cannot evaluate `[init]`",
        )
    ):
        raise LeanFailure("environment_failure", log or diagnostics)
    if mode == "parse" and result.get("parse_failed"):
        raise LeanFailure("unsupported_syntax", str(result["errors"]))
    result["log"] = log
    return result


def inspect(
    source,
    environment,
    runtime=None,
    mode="parse",
    target="",
    timeout=None,
    memory_mb=4096,
):
    runtime = runtime or {}
    timeout = timeout if timeout is not None else runtime.get("parse_timeout", 60)
    if not isinstance(timeout, int) or timeout <= 0:
        raise LeanFailure("invalid_resource_configuration", "timeout must be positive")
    memory_mb = runtime.get("memory_mb", memory_mb)
    if not isinstance(memory_mb, int) or memory_mb <= 0:
        raise LeanFailure(
            "invalid_resource_configuration", "memory_mb must be positive"
        )
    runner = runtime.get("runner", "process")
    if runner not in {"process", "worker"}:
        raise LeanFailure("invalid_resource_configuration", "unknown checker runner")
    if runner == "worker":
        if (
            type(runtime.get("worker_max_cases", 512)) is not int
            or runtime.get("worker_max_cases", 512) <= 0
        ):
            raise LeanFailure(
                "invalid_resource_configuration", "worker_max_cases must be positive"
            )
        from .worker import inspect_with_worker

        return inspect_with_worker(
            source, environment, runtime, mode, target, timeout, memory_mb
        )
    tools, project, env = execution_config(environment, runtime)
    lean = tools / "bin" / "lean"
    with tempfile.TemporaryDirectory(prefix="corpus-check-") as temporary:
        tmp = Path(temporary)
        task = tmp / "Task.lean"
        task.write_text(source)
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
                str(tools),
                str(HELPER),
                str(project),
            ],
            "write": temporary,
            "cwd": str(project),
            "command": [
                str(lean),
                "-j1",
                "--run",
                str(HELPER),
                str(task),
                mode,
                target,
            ],
            "env": env,
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
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
            resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))

        try:
            with (tmp / "stdout").open("w+") as out, (tmp / "stderr").open("w+") as err:
                process = subprocess.Popen(
                    command,
                    cwd=project,
                    env=env,
                    stdout=out,
                    stderr=err,
                    preexec_fn=limits,
                )
                try:
                    process.wait(timeout=timeout + 3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise LeanFailure("timeout")
                out.seek(0)
                err.seek(0)
                log = out.read(1024**2) + err.read(1024**2)
        except OSError as e:
            raise LeanFailure("infrastructure_failure", str(e)) from e
        if "CORPUS_SANDBOX_ERROR" in log or "unshare:" in log:
            raise LeanFailure("isolation_unavailable", log)
        if process.returncode != 0:
            reason = (
                "resource_exhaustion"
                if process.returncode < 0 or "memory" in log.lower()
                else "parser_or_import_failure"
            )
            raise LeanFailure(reason, f"checker exit code {process.returncode}\n" + log)
        lines = [
            line[len("CORPUS_JSON:") :]
            for line in log.splitlines()
            if line.startswith("CORPUS_JSON:")
        ]
        if len(lines) != 1:
            raise LeanFailure("inspection_protocol_failure", log)
        try:
            result = json.loads(lines[0])
        except (ValueError, TypeError) as error:
            raise LeanFailure("inspection_protocol_failure", log) from error
        return decode_result(result, mode, log)


def replace_span(source, span, replacement):
    data = source.encode()
    return (data[: span[0]] + replacement.encode() + data[span[1] :]).decode()


def slice_span(source, span):
    return source.encode()[span[0] : span[1]].decode()


def target_info(source, environment, runtime=None, name=None):
    parsed = inspect(source, environment, runtime)
    targets = (
        [t for t in parsed["targets"] if t["name"] == name]
        if name
        else parsed["targets"]
    )
    if len(targets) != 1:
        raise ValueError("unsupported: expected exactly one theorem target")
    target = targets[0]
    if "unsupported" in target:
        raise ValueError(target["unsupported"])
    # Count parser atoms, not textual occurrences in comments or strings.
    body = slice_span(source, target["body"])
    if body != "by sorry" or parsed["admissions"] != 1:
        raise ValueError(
            "unsupported: task requires one target 'by sorry' and complete supporting context"
        )
    return target
