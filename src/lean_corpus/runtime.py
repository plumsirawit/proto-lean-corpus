"""Validate pinned runtime inputs once per stage, then recheck before publication."""

from pathlib import Path
from .common import file_hash, identity, read_json


def validate_runtime(environment, runtime):
    if not environment["resolved"]:
        return
    if environment["packages"] or environment["build_config"]:
        if not runtime.get("project_dir"):
            raise ValueError("recorded project requires a prepared --runtime mapping")
        root = Path(runtime["project_dir"])
        for entry in environment.get("project_files", []):
            p = root / entry["path"]
            if not p.is_file() or file_hash(p) != entry["sha256"]:
                raise ValueError("runtime source differs: " + entry["path"])
        for name, content in environment["build_config"].items():
            p = root / name
            if not p.is_file() or p.read_text() != content:
                raise ValueError(f"runtime configuration differs: {name}")
        for package in environment["packages"]:
            import subprocess

            p = root / ".lake" / "packages" / package["name"]
            try:
                rev = subprocess.check_output(
                    ["git", "-C", str(p), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                dirty = subprocess.check_output(
                    ["git", "-C", str(p), "status", "--porcelain"], text=True
                )
            except (OSError, subprocess.CalledProcessError) as e:
                raise ValueError("dependency unavailable: " + package["name"]) from e
            if rev != package["rev"] or dirty:
                raise ValueError("dependency differs: " + package["name"])


class RuntimeGuard:
    def __init__(self, work):
        self.work = Path(work)
        self.contexts = {}
        self.failures = {}

    def get(self, environment_id):
        if environment_id in self.failures:
            raise ValueError(self.failures[environment_id])
        if environment_id not in self.contexts:
            environment = read_json(
                self.work / "environments" / f"{environment_id}.json"
            )
            path = self.work / "runtimes" / f"{environment_id}.json"
            runtime = read_json(path) if path.exists() else {}
            try:
                if identity("environment", environment) != environment_id:
                    raise ValueError("environment manifest identity mismatch")
                validate_runtime(environment, runtime)
            except ValueError as error:
                self.failures[environment_id] = str(error)
                raise
            self.contexts[environment_id] = (environment, runtime)
        return self.contexts[environment_id]

    def finish(self):
        for environment_id, (environment, runtime) in self.contexts.items():
            path = self.work / "runtimes" / f"{environment_id}.json"
            current_runtime = read_json(path) if path.exists() else {}
            current_environment = read_json(
                self.work / "environments" / f"{environment_id}.json"
            )
            if current_environment != environment or current_runtime != runtime:
                raise ValueError("runtime mapping or environment changed during stage")
            validate_runtime(environment, runtime)
