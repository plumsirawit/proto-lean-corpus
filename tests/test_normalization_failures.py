"""Failure injection checks stage completeness; no mocked kernel success."""

from pathlib import Path
import pytest
from lean_corpus.acquire import acquire, check_snapshot
from lean_corpus.common import read_json, write_json
from lean_corpus.deduplicate import deduplicate, require_current
from lean_corpus.lean import LeanFailure
from lean_corpus.normalize import normalize
from lean_corpus.schema import read_tables


def test_operational_failure_aborts_and_resume_preserves_records(tmp_path, monkeypatch):
    work = tmp_path / "work"
    fixture = Path("tests/fixtures/numina").resolve()
    sid = acquire(work, "configs/sources.yaml", "numina_math_lean", fixture)
    env = check_snapshot(work / "snapshots" / sid)["environment_id"]
    write_json(
        work / "runtimes" / f"{env}.json",
        {"project_dir": str(fixture), "lean_path": []},
    )
    calls = []
    fail = True

    def adapter(row, environment, runtime):
        nonlocal fail
        calls.append(row["uuid"])
        if row["uuid"] == "fixture-2" and fail:
            raise LeanFailure("timeout", "injected infrastructure failure")
        task = row["formal_statement"]
        if task.endswith(" by"):
            task += " sorry"
        return {
            "task": task,
            "target": task.split("theorem ", 1)[1].split()[0],
            "informal": row["problem"],
            "upstream_id": row["uuid"],
            "attempts": [],
            "reports": [],
            "transformations": [],
            "_parser_imports_resolved": True,
        }

    monkeypatch.setitem(
        __import__("lean_corpus.normalize", fromlist=["ADAPTERS"]).ADAPTERS,
        "numina_math_lean",
        adapter,
    )
    with pytest.raises(LeanFailure):
        normalize(work, "configs/sources.yaml")
    assert not (work / "normalized/state.json").exists()
    assert read_json(work / "normalization-incomplete.json")["state"] == "failed"
    with pytest.raises(ValueError, match="incomplete"):
        deduplicate(work)
    with pytest.raises(ValueError, match="incomplete"):
        require_current(work)
    fail = False
    assert normalize(work, "configs/sources.yaml")["problems"] == 3
    assert calls.count("fixture-1") == 1  # completed extraction resumed from cache
    first = read_tables(work / "normalized")
    assert not (work / "normalization-incomplete.json").exists()
    before = len(calls)
    normalize(work, "configs/sources.yaml")
    assert len(calls) == before
    assert read_tables(work / "normalized") == first
    assert read_json(work / "reports/extraction.json")["cache_hits"] == 3
    deduplicate(work)
    require_current(work)
    selection = tmp_path / "selection.json"
    manifest = {"schema_version": "1", "snapshots": {sid: {"data.jsonl": [1]}}}
    # Resolve the original fixture filename rather than creating sampled payloads.
    original = work / "snapshots" / sid / "original"
    relative = next(original.glob("*.jsonl")).name
    manifest["snapshots"][sid] = {relative: [1]}
    write_json(selection, manifest)
    assert normalize(work, "configs/sources.yaml", selection=selection)["problems"] == 1
    chosen = read_tables(work / "normalized")["problems"][0]
    assert f"{relative}#row=1&field=" in chosen["source_refs"][0]["location"]
    assert read_json(work / "reports/extraction.json")["selection"] == manifest
    manifest["snapshots"][sid][relative] = [1000]
    write_json(selection, manifest)
    with pytest.raises(ValueError, match="missing or unprocessed"):
        normalize(work, "configs/sources.yaml", selection=selection)
    with pytest.raises(ValueError, match="incomplete"):
        deduplicate(work)
    normalize(work, "configs/sources.yaml")
    cache = next((work / "extraction-cache").glob("*.json"))
    cache.write_text("{}")
    with pytest.raises(RuntimeError, match="integrity"):
        normalize(work, "configs/sources.yaml")
    assert (work / "normalization-incomplete.json").exists()
