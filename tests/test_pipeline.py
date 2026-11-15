from pathlib import Path
import json
import shutil
import pytest
from lean_corpus.acquire import acquire, check_snapshot, registry
from lean_corpus.common import read_json
from lean_corpus.normalize import normalize
from lean_corpus.deduplicate import deduplicate
from lean_corpus.verify import verify
from lean_corpus.export import export
from lean_corpus.schema import read_tables

REGISTRY = Path("configs/sources.yaml")


def test_acquisition_integrity_and_deferred(tmp_path):
    work = tmp_path / "work"
    sid = acquire(work, REGISTRY, "numina_math_lean", Path("tests/fixtures/numina"))
    assert (
        acquire(work, REGISTRY, "numina_math_lean", Path("tests/fixtures/numina"))
        == sid
    )
    assert registry(REGISTRY)["ace_reason_math"]["status"] == "deferred"
    assert len([s for s in registry(REGISTRY).values() if s["status"] == "active"]) == 8
    with pytest.raises(ValueError, match="deferred"):
        acquire(work, REGISTRY, "ace_reason_math", Path("tests/fixtures/numina"))
    (work / "snapshots" / sid / "original" / "data.jsonl").write_text("corrupted")
    with pytest.raises(ValueError, match="integrity"):
        check_snapshot(work / "snapshots" / sid)


def test_record_extraction_limit_preserves_other_inputs(tmp_path, monkeypatch):
    from lean_corpus.adapters import ADAPTERS
    from lean_corpus.lean import LeanFailure

    local = tmp_path / "source"
    local.mkdir()
    (local / "data.jsonl").write_text('{"id":"limited"}\n{"id":"kept"}\n')
    work = tmp_path / "work"
    acquire(work, REGISTRY, "goedel_pset_v1", local)

    def extract(row, _environment, _runtime):
        if row["id"] == "limited":
            raise LeanFailure("timeout")
        return {
            "task": "theorem kept : True := by sorry",
            "target": "kept",
            "informal": None,
            "upstream_id": row["id"],
            "attempts": [],
            "reports": [],
            "transformations": [],
        }

    monkeypatch.setitem(ADAPTERS, "goedel_pset_v1", extract)
    assert normalize(work, REGISTRY) == {"problems": 1, "proofs": 0, "reports": 1}
    report = read_json(work / "reports/extraction.json")
    assert report["input_occurrences"] == 2
    [limited] = report["reports"]
    assert limited["kind"] == "inconclusive"
    assert limited["location"] == "data.jsonl#row=0"
    assert limited["reason"] == "timeout"


@pytest.mark.lean
def test_end_to_end(real_lean, tmp_path, monkeypatch):
    work = tmp_path / "work"
    sid = acquire(work, REGISTRY, "numina_math_lean", Path("tests/fixtures/numina"))
    manifest = check_snapshot(work / "snapshots" / sid)
    env_id = manifest["environment_id"]
    # The runtime is an immutable matching config; no packages are needed here.
    from lean_corpus.common import write_json

    write_json(
        work / "runtimes" / f"{env_id}.json",
        {"project_dir": str(Path("tests/fixtures/numina").resolve()), "lean_path": []},
    )
    stats = normalize(work, REGISTRY)
    assert stats == {"problems": 3, "proofs": 4, "reports": 0}
    assert deduplicate(work)["problems"] == 3
    statuses = verify(work)
    assert statuses == {"unverified": 0, "valid": 2, "invalid": 2, "inconclusive": 0}, (
        statuses
    )
    quality = export(work, tmp_path / "release")
    assert quality["counts"] == {"problems": 3, "proofs": 4, "provenance": 1}
    shutil.move(tmp_path / "release", tmp_path / "relocated")
    output = tmp_path / "relocated"
    tables = read_tables(output)
    for provenance in tables["provenance"]:
        for ref in provenance["license_refs"]:
            assert (output / ref).is_file()
    assert len(read_tables(output / "views" / "verified")["proofs"]) == 2
    assert str(work) not in json.dumps(read_json(output / "quality.json"))
    # A resumed run reuses both real completed proof and statement checks.
    import lean_corpus.verify as verifier
    from lean_corpus.check_cache import CheckCacheCorruption

    assert len(list((work / "statement-cache").glob("*.json"))) == 3
    with monkeypatch.context() as context:

        def unexpected_check(*args, **kwargs):
            pytest.fail("completed inspection was not resumed from cache")

        context.setattr(verifier, "check_problem", unexpected_check)
        context.setattr(verifier, "check_proof", unexpected_check)
        assert verify(work) == statuses
    # A damaged cache cannot retain a terminal validity claim in an export.
    for cache in (work / "verification-cache").glob("*.json"):
        record = read_json(cache)
        if record["result"]["status"] == "valid":
            record["result"]["reason"] = "damaged cache"
            write_json(cache, record)
            break
    with pytest.raises(CheckCacheCorruption):
        verify(work)
    damaged = export(work, tmp_path / "damaged-cache-release")
    assert len(damaged["stale_statuses_reset"]) == 1
    assert damaged["verification_status"]["unverified"] == 1
    assert normalize(work, REGISTRY) == stats
    deduplicate(work)
    assert len(read_tables(work / "canonical")["proofs"]) == 4


@pytest.mark.lean
def test_workbook_json_parquet_overlap(real_lean, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    local = tmp_path / "source"
    local.mkdir()
    row = {
        "id": "wb",
        "formal_statement": "theorem t : True := by sorry",
        "tactic": "trivial",
    }
    (local / "data.json").write_text(json.dumps([row]))
    pq.write_table(pa.Table.from_pylist([row]), local / "data.parquet")
    (local / "LICENSE").write_text("Synthetic Apache-2.0 fixture")
    work = tmp_path / "work"
    acquire(work, REGISTRY, "lean_workbook", local)
    stats = normalize(work, REGISTRY)
    assert stats["problems"] == 2 and stats["proofs"] == 2
    deduplicate(work)
    data = read_tables(work / "canonical")
    assert len(data["problems"]) == len(data["proofs"]) == 1
    assert len(data["problems"][0]["source_refs"]) == 2
    assert len(data["proofs"][0]["source_refs"]) == 2
    assert verify(work)["inconclusive"] == 1


def test_bounded_hf_acquisition_preserves_notices(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import huggingface_hub

    names = [
        "data/selected.parquet",
        "legal/license.txt",
        "AUTHORS",
        "docs/method.md",
        "NOTICE",
    ]
    commit = "a" * 40

    class API:
        def dataset_info(self, repo, revision):
            assert revision == "chosen-ref"
            return SimpleNamespace(
                sha=commit, siblings=[SimpleNamespace(rfilename=n) for n in names]
            )

    def download(repo, repo_type, revision, local_dir, allow_patterns):
        assert revision == commit and repo_type == "dataset"
        assert {
            "data/selected.parquet",
            "legal/license.txt",
            "AUTHORS",
            "docs/method.md",
            "NOTICE",
        } <= set(allow_patterns)
        for name in names:
            p = Path(local_dir) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("synthetic payload")

    monkeypatch.setattr(huggingface_hub, "HfApi", API)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    work = tmp_path / "work"
    sid = acquire(
        work,
        REGISTRY,
        "goedel_pset_v1",
        revision="chosen-ref",
        patterns=["data/selected.parquet"],
    )
    manifest = check_snapshot(work / "snapshots" / sid)
    assert manifest["revision"] == commit
    assert "legal/license.txt" in manifest["license_paths"]
    assert "AUTHORS" in manifest["license_paths"]


@pytest.mark.lean
def test_keyed_workbook_selection_retains_and_checks_proof_list(real_lean, tmp_path):
    from lean_corpus.common import write_json

    local = tmp_path / "source"
    local.mkdir()
    write_json(
        local / "keyed.json",
        {
            "chosen": {
                "id": "example",
                "formal_statement": "theorem keyed_target : True := by sorry",
                "proof": ["trivial", "by sorry", "exact True.intro", 17, ""],
            },
            "excluded": {"formal_statement": "malformed excluded record"},
        },
    )
    (local / "lean-toolchain").write_text(real_lean["toolchain"])
    work = tmp_path / "work"
    sid = acquire(work, REGISTRY, "lean_workbook", local)
    eid = check_snapshot(work / "snapshots" / sid)["environment_id"]
    write_json(work / "runtimes" / f"{eid}.json", {"project_dir": str(local)})
    selection = tmp_path / "selection.json"
    write_json(
        selection,
        {"schema_version": "1", "snapshots": {sid: {"keyed.json": ["chosen"]}}},
    )
    assert normalize(work, REGISTRY, selection=selection) == {
        "problems": 1,
        "proofs": 3,
        "reports": 1,
    }
    tables = read_tables(work / "normalized")
    assert {p["source_refs"][0]["location"] for p in tables["proofs"]} == {
        f"keyed.json#row=chosen&field=proof/{i}" for i in range(3)
    }
    report = read_json(work / "reports/extraction.json")
    assert report["reports"][0]["location"] == "keyed.json#row=chosen&field=proof/3"
    deduplicate(work)
    assert verify(work) == {
        "unverified": 0,
        "valid": 2,
        "invalid": 1,
        "inconclusive": 0,
    }
