import shutil
from pathlib import Path

import pytest

from lean_corpus.acquire import acquire
from lean_corpus.common import read_json, write_json
from lean_corpus.deduplicate import deduplicate
from lean_corpus.normalize import normalize
from lean_corpus.parallel_verify import verify_parallel
from lean_corpus.schema import read_tables
from lean_corpus.verify import verify
from lean_corpus.worker import close_all


def semantic_report(value):
    # Raw worker logs include request sequence numbers and measured RSS. These
    # legitimately change with scheduling; every structured Lean result and
    # diagnostic must still agree. Raw logs remain intact in reports/caches.
    if isinstance(value, dict):
        return {k: semantic_report(v) for k, v in value.items() if k != "log"}
    if isinstance(value, list):
        return [semantic_report(v) for v in value]
    return value


@pytest.mark.lean
@pytest.mark.parametrize("runner", ["process", "worker"])
def test_parallel_matches_serial_and_resumes(real_lean, tmp_path, monkeypatch, runner):
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "2")
    work = tmp_path / "serial"
    registry = Path("configs/sources.yaml")
    sid = acquire(work, registry, "numina_math_lean", Path("tests/fixtures/numina"))
    env_id = read_json(work / "snapshots" / sid / "manifest.json")["environment_id"]
    write_json(
        work / "runtimes" / f"{env_id}.json",
        {
            "project_dir": str(Path("tests/fixtures/numina").resolve()),
            "lean_path": [],
            "runner": runner,
        },
    )
    normalize(work, registry)
    deduplicate(work)
    parallel = tmp_path / "parallel"
    shutil.copytree(work, parallel)
    expected = verify(work)
    import subprocess
    from lean_corpus.worker import _WORKERS

    parents = list(_WORKERS.values())
    if runner == "worker":
        assert parents
    popen = subprocess.Popen

    def spawn_after_releasing_parent(*args, **kwargs):
        assert all(w.process is None or w.process.poll() is not None for w in parents)
        return popen(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(subprocess, "Popen", spawn_after_releasing_parent)
        assert verify_parallel(parallel, workers=2) == expected
    assert read_tables(parallel / "canonical") == read_tables(work / "canonical")
    assert semantic_report(
        read_json(parallel / "reports/verification.json")
    ) == semantic_report(read_json(work / "reports/verification.json"))
    caches = {
        str(p.relative_to(parallel)): p.read_bytes()
        for name in ("statement-cache", "verification-cache")
        for p in (parallel / name).glob("*.json")
    }
    # Resume a moved checkpoint: old shard symlinks must be recreated and the
    # unchanged checker must reuse serial-compatible cache identities.
    moved = tmp_path / "moved"
    shutil.move(parallel, moved)
    assert verify_parallel(moved, workers=2) == expected
    assert {
        str(p.relative_to(moved)): p.read_bytes()
        for name in ("statement-cache", "verification-cache")
        for p in (moved / name).glob("*.json")
    } == caches
    for limit in (1, 0):
        assert verify_parallel(moved, workers=2, limit=limit) == verify(
            work, limit=limit
        )
        assert semantic_report(
            read_json(moved / "reports/verification.json")
        ) == semantic_report(read_json(work / "reports/verification.json"))
    before = read_tables(moved / "canonical")
    cache = next((moved / "verification-cache").glob("*.json"))
    cache.write_text("corrupt")
    with pytest.raises(RuntimeError, match="shard failed"):
        verify_parallel(moved, workers=2)
    assert read_tables(moved / "canonical") == before


def test_parallel_rejects_overallocation(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "1")
    with pytest.raises(ValueError, match="allocated CPUs"):
        verify_parallel(tmp_path, workers=2)
