"""Process-isolated verification shards using the unchanged serial checker/cache keys."""

import argparse
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .common import file_hash, read_json, write_json
from .deduplicate import require_current
from .lean import POLICY
from .runtime import RuntimeGuard
from .schema import read_tables, write_tables
from .verify import verify
from .worker import close_all


def verify_parallel(work, workers=1, limit=None, timeout=60):
    if workers < 1 or timeout <= 0 or (limit is not None and limit < 0):
        raise ValueError("workers/timeout must be positive and limit nonnegative")
    if workers == 1:
        return verify(work, limit, timeout)
    allocation = int(
        os.environ.get("SLURM_CPUS_PER_TASK", len(os.sched_getaffinity(0)))
    )
    if workers > allocation:
        raise ValueError("verification workers exceed allocated CPUs")
    work = Path(work).resolve()
    with (work / ".parallel-verification.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _verify(work, workers, limit, timeout)


def _verify(work, workers, limit, timeout):
    require_current(work)
    tables = read_tables(work / "canonical")
    # Detect concurrent input/status edits before committing any merged result.
    originals = {
        path: file_hash(path)
        for directory in ("canonical", "normalized")
        for path in (work / directory).glob("*")
        if path.is_file()
    }
    selected_proofs = tables["proofs"][:limit]
    selected_ids = {p["id"] for p in tables["problems"][:limit]} | {
        p["problem_id"] for p in selected_proofs
    }
    selected = [p for p in tables["problems"] if p["id"] in selected_ids]
    guard = RuntimeGuard(work)
    for env_id in {p["environment_id"] for p in selected}:
        try:
            guard.get(env_id)
        except ValueError:
            pass  # The serial checker records these as inconclusive.
    cache_dirs = [work / name for name in ("statement-cache", "verification-cache")]
    for directory in cache_dirs:
        directory.mkdir(exist_ok=True)
    previous_caches = {p for d in cache_dirs for p in d.glob("*.json")}
    root = work / "parallel-verification"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    shards = [root / f"shard-{i:03d}" for i in range(min(workers, len(selected)))]
    owners = {p["id"]: i % len(shards) for i, p in enumerate(selected)}
    children = []
    try:
        for i, shard in enumerate(shards):
            shard.mkdir()
            # Each problem and all its attempts have exactly one owner. Cache
            # keys therefore cannot collide between concurrent writers. Symlinks
            # keep completed caches in the ordinary checkpointed corpus roots.
            for name in (
                "environments",
                "runtimes",
                "statement-cache",
                "verification-cache",
            ):
                (shard / name).symlink_to(work / name, target_is_directory=True)
            write_tables(
                shard / "canonical",
                {
                    "problems": [p for p in selected if owners[p["id"]] == i],
                    "proofs": [
                        p for p in selected_proofs if owners[p["problem_id"]] == i
                    ],
                    "provenance": tables["provenance"],
                },
            )
            state = read_json(work / "canonical/state.json")
            for name in ("canonical", "normalized"):
                write_json(shard / name / "state.json", state)
            log = (shard / "worker.log").open("w")
            try:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        __name__,
                        "--shard",
                        str(shard),
                        "--timeout",
                        str(timeout),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            finally:
                log.close()
            children.append(child)
        while any(child.poll() is None for child in children):
            if any(child.poll() not in (None, 0) for child in children):
                raise RuntimeError(
                    "verification shard failed; see parallel-verification/*/worker.log"
                )
            time.sleep(0.2)
        if any(child.returncode != 0 for child in children):
            raise RuntimeError(
                "verification shard failed; see parallel-verification/*/worker.log"
            )
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    try:
        guard.finish()
        require_current(work)
        if any(file_hash(p) != h for p, h in originals.items()):
            raise ValueError("verification inputs changed during parallel stage")
    except BaseException:
        for directory in cache_dirs:
            for path in directory.glob("*.json"):
                if path not in previous_caches:
                    path.unlink(missing_ok=True)
        raise
    attempts, problems = {}, {}
    for shard in shards:
        report = read_json(shard / "reports/verification.json")
        attempts.update((r["proof_id"], r) for r in report["attempts"])
        problems.update(report["problems"])
    if (
        set(attempts) != {p["id"] for p in selected_proofs}
        or set(problems) != selected_ids
    ):
        raise ValueError("incomplete parallel verification coverage")
    for proof in selected_proofs:
        proof["verification_status"] = attempts[proof["id"]]["status"]
    write_tables(work / "canonical", tables)
    write_json(
        work / "reports/verification.json",
        {
            "policy": POLICY,
            "limit": limit,
            "attempts": [attempts[p["id"]] for p in selected_proofs],
            "problems": problems,
        },
    )
    return {
        status: sum(p["verification_status"] == status for p in tables["proofs"])
        for status in ("unverified", "valid", "invalid", "inconclusive")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=Path, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise InterruptedError(f"verification interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        verify(args.shard, timeout=args.timeout)
    finally:
        close_all()


if __name__ == "__main__":
    main()
