"""Output equivalence and failed-publication checks; no simulated kernel claims."""

import json
from pathlib import Path

import pytest

from lean_corpus.common import canonical, digest, read_json
from lean_corpus.disk_output import (
    RowSpool, spool_digest, write_document, write_spooled_tables,
)
from lean_corpus.schema import read_tables, write_tables
from test_schema_dedup import problem, proof, tables


def serial_tables():
    p = problem(informal="∀ α\nUnicode")
    data = tables([p, p], [proof(p), proof(p, "by sorry")])
    for row in data["provenance"]:
        row["acquired_at"] = row["acquired_at"].isoformat()
    return data


def test_spooled_output_and_fingerprint_equal_existing(tmp_path):
    original = serial_tables()
    spooled = {}
    for name, rows in original.items():
        spooled[name] = RowSpool(tmp_path / f"{name}.jsonl")
        spooled[name].extend(iter(rows))
    assert spool_digest(spooled) == digest(original)
    for name in spooled:
        assert list(spooled[name]) == list(spooled[name]) == original[name]
        assert len(spooled[name]) == len(original[name])
    document = {"tables": spooled, "null": None, "selection": [1, "α"]}
    write_document(tmp_path / "report.json", document)
    assert (tmp_path / "report.json").read_text() == canonical(
        {**document, "tables": original}
    ) + "\n"
    write_tables(tmp_path / "old", original, unique=False)
    counts = write_spooled_tables(tmp_path / "new", spooled,
                                  tmp_path / "stage", batch_size=1)
    assert counts == {name: len(rows) for name, rows in original.items()}
    assert read_tables(tmp_path / "old") == read_tables(tmp_path / "new")


@pytest.mark.parametrize("failure", ["key", "null", "status", "iterator"])
def test_failed_spooling_keeps_previous_tables(tmp_path, failure):
    data = serial_tables()
    public = tmp_path / "public"
    write_tables(public, data, unique=False)
    before = {p.name: p.read_bytes() for p in public.iterdir()}
    if failure == "key":
        data["proofs"][0]["problem_id"] = "missing"
    elif failure == "null":
        data["proofs"][0]["formal_proof"] = None
    elif failure == "status":
        data["proofs"][0]["verification_status"] = "unknown"
    else:
        first = data["proofs"][0]

        def broken():
            yield first
            raise RuntimeError("injected interrupted output")
        data["proofs"] = broken()
    with pytest.raises((ValueError, RuntimeError)):
        write_spooled_tables(public, data, tmp_path / "failed-stage")
    assert {p.name: p.read_bytes() for p in public.iterdir()} == before
    assert (tmp_path / "failed-stage").exists()


def test_empty_spools_and_exclusive_reuse(tmp_path):
    spools = {name: RowSpool(tmp_path / f"{name}.jsonl")
              for name in serial_tables()}
    assert spool_digest(spools) == digest(dict.fromkeys(spools, []))
    write_spooled_tables(tmp_path / "out", spools, tmp_path / "stage")
    assert read_tables(tmp_path / "out") == dict.fromkeys(spools, [])
    with pytest.raises(FileExistsError):
        RowSpool(tmp_path / "proofs.jsonl")
    with pytest.raises(FileExistsError):
        write_spooled_tables(tmp_path / "out", spools, tmp_path / "stage")


@pytest.mark.parametrize("spooled", [False, True])
def test_normalization_failure_blocks_downstream(tmp_path, monkeypatch, spooled):
    # Reuse the established failure/resume test in both output modes.
    from test_normalization_failures import (
        test_operational_failure_aborts_and_resume_preserves_records,
    )
    import test_normalization_failures as fixture
    normalize = fixture.normalize
    generation = 0

    def run(*args, **kwargs):
        nonlocal generation
        generation += 1
        if spooled:
            kwargs["spool_dir"] = tmp_path / f"spool-{generation}"
        return normalize(*args, **kwargs)

    monkeypatch.setattr(fixture, "normalize", run)
    test_operational_failure_aborts_and_resume_preserves_records(tmp_path, monkeypatch)


def test_normalization_matches_tables_reports_and_state(tmp_path, monkeypatch):
    from lean_corpus.acquire import acquire, check_snapshot
    from lean_corpus.common import write_json
    import lean_corpus.normalize as module

    work = tmp_path / "work"
    fixture = Path("tests/fixtures/numina").resolve()
    sid = acquire(work, "configs/sources.yaml", "numina_math_lean", fixture)
    env = check_snapshot(work / "snapshots" / sid)["environment_id"]
    write_json(work / "runtimes" / f"{env}.json",
               {"project_dir": str(fixture), "lean_path": []})

    def adapter(row, environment, runtime):
        return {"task": row["formal_statement"], "target": "t", "informal": None,
                "upstream_id": row["uuid"], "attempts": [],
                "reports": [{"field": "proof", "kind": "unsupported", "reason": "α"}],
                "transformations": ["preserved fixture"],
                "_parser_imports_resolved": False}

    monkeypatch.setitem(module.ADAPTERS, "numina_math_lean", adapter)
    module.normalize(work, "configs/sources.yaml")  # Warm the same extraction cache.
    expected = module.normalize(work, "configs/sources.yaml")
    before = read_tables(work / "normalized")
    reports = {p.name: read_json(p) for p in (work / "reports").glob("*.json")}
    state = read_json(work / "normalized/state.json")
    actual = module.normalize(work, "configs/sources.yaml", spool_dir=tmp_path / "spools")
    assert actual == expected
    assert read_tables(work / "normalized") == before
    assert {p.name: json.loads(p.read_text())
            for p in (work / "reports").glob("*.json")} == reports
    assert read_json(work / "normalized/state.json") == state
