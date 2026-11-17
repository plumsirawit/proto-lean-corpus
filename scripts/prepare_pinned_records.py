"""Compute-node preparation of downloaded original records and nested selections.

The public upstream tree supplies LFS SHA256 or Git blob SHA1 for every file.
The resulting local snapshot keeps its content identity and upstream revision label.
No downloads, source rewriting, or acceptance-policy changes occur here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pyarrow.parquet as pq

from lean_corpus.acquire import acquire, inventory
from lean_corpus.common import file_hash, write_json
from lean_corpus.lean import inspect
from lean_corpus.runtime import RuntimeGuard
from lean_corpus.worker import close_all
from euler_quota import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()
    source = args.source
    evidence = args.evidence
    pin = json.loads((evidence / f"{source}-pin.json").read_text())
    entries = [
        e for e in json.loads((evidence / f"{source}-sizes.json").read_text())
        if e["type"] == "file"
    ]
    assert source == pin["source"]
    assert {e["path"] for e in entries} == set(pin["files"])
    assert {p.relative_to(args.raw).as_posix() for p in args.raw.rglob("*") if p.is_file()} == set(pin["files"])
    hashes = {}
    rows = {}
    for entry in entries:
        path = args.raw / entry["path"]
        assert not path.is_symlink() and path.stat().st_size == entry["size"]
        sha256 = file_hash(path)
        if "lfs" in entry:
            assert sha256 == entry["lfs"]["oid"], entry["path"]
        else:
            blob = hashlib.sha1(f"blob {entry['size']}\0".encode())
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    blob.update(chunk)
            assert blob.hexdigest() == entry["oid"], entry["path"]
        hashes[entry["path"]] = sha256
        if path.suffix == ".parquet":
            rows[entry["path"]] = pq.ParquetFile(path).metadata.num_rows
        print("VERIFIED", entry["path"], sha256, flush=True)
    selection = json.loads((evidence / "goedel-selection-plan.json").read_text())
    assert selection["source"] == source and selection["revision"] == pin["revision"]
    assert sum(rows.values()) == selection["source_occurrences"]
    workspace = Path(os.environ["TMPDIR"]) / source
    work = workspace / "corpus"
    acquire(work, "configs/sources.yaml", source, local=args.raw, revision=pin["revision"])
    [snapshot] = (work / "snapshots").iterdir()
    saved = json.loads((snapshot / "manifest.json").read_text())
    assert saved["revision_label"] == pin["revision"]
    assert {e["path"]: e["sha256"] for e in saved["files"]} == hashes
    previous = set()
    for size in (10, 100, 1000):
        chosen = selection["selection_by_size"][str(size)]
        pairs = {(path, row) for path, indices in chosen.items() for row in indices}
        assert previous <= pairs and len(pairs) == min(size, sum(rows.values()))
        assert all(0 <= row < rows[path] for path, row in pairs)
        write_json(workspace / f"selection-{size}.json", {
            "schema_version": "1", "snapshots": {snapshot.name: chosen},
        })
        previous = pairs
    write_json(workspace / "upstream-integrity.json", {
        "pin": pin, "sha256": hashes, "actual_parquet_rows": rows,
        "source_occurrences": sum(rows.values()), "selection_plan": selection,
    })
    project = Path(os.environ["LC_LOCAL_PROJECT"])
    expected_revision = "2f65ba7f1a9144b20c8e7358513548e317d26de1"
    # Recovery archives intentionally omit the root .git. Compare every source
    # file to the exact committed tree in the preserved read-only upstream clone;
    # copying Git configs/credentials into the job is unnecessary.
    upstream_clone = Path(os.environ["LC_UPSTREAM_CLONE"])
    tree = subprocess.check_output([
        "git", "-C", str(upstream_clone), "ls-tree", "-r", "-z", expected_revision,
    ], timeout=60)
    expected = {}
    for item in tree.split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        assert kind == "blob" and mode in {"100644", "100755"}
        expected[name.decode()] = oid
    assert set(expected) == {e["path"] for e in inventory(project)}
    for name, oid in expected.items():
        path = project / name
        blob = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                blob.update(chunk)
        assert blob.hexdigest() == oid, name
    write_json(workspace / "project-git-integrity.json", {
        "revision": expected_revision, "git_tree": expected,
        "all_archived_project_files_match_pinned_git_tree": True,
    })
    for name in ("lean-toolchain", "lake-manifest.json", "lakefile.lean"):
        assert (project / name).read_bytes() == (evidence / ("goedel-mathlib-" + name)).read_bytes()
    os.environ["LC_LOCAL_WORKSPACE"] = str(workspace)
    subprocess.run([os.sys.executable, "scripts/resolve_local_runtime.py"], check=True)
    guard = RuntimeGuard(work)
    [runtime] = (work / "runtimes").glob("*.json")
    environment, mapping = guard.get(runtime.stem)
    probe = inspect("import Mathlib\ntheorem __preparation_probe : True := by trivial", environment, mapping, "verify", "__preparation_probe", 180)
    write_json(workspace / "environment-probe.json", probe)
    assert not probe["errors"] and probe.get("is_theorem") and not probe.get("axioms"), probe
    guard.finish()
    close_all()
    # Only compact archives go to shared scratch. Originals remain untouched.
    total = sum(e["size"] for e in entries)
    check(evidence, 2 * 1024**2, 10)
    check(args.stage, total + 128 * 1024**2, 5)
    archive = args.stage / f"input-{source}-{os.environ['SLURM_JOB_ID']}.tar"
    assert not archive.exists()
    partial = archive.with_suffix(".tar.partial")
    with tarfile.open(partial, "w") as output:
        output.add(workspace, arcname=source)
    partial.replace(archive)
    checksum = file_hash(archive)
    Path(str(archive) + ".sha256").write_text(f"{checksum}  {archive}\n")
    # Read back the saved archive in full before recording success.
    assert file_hash(archive) == checksum
    receipt = {
        "complete": True, "archive": str(archive), "sha256": checksum,
        "job_id": os.environ["SLURM_JOB_ID"], "source": source,
        "snapshot_id": snapshot.name, "source_occurrences": sum(rows.values()),
        "upstream_revision": pin["revision"], "code_revision": os.environ["LC_CODE_REVISION"],
        "environment_id": runtime.stem, "mathlib_revision": expected_revision,
        "checks": ["upstream_lfs_sha256_and_git_blob", "actual_parquet_counts", "nested_original_rows", "exact_project_and_dependency_locks", "real_kernel_probe", "saved_archive_sha256_readback"],
    }
    write_json(Path(str(archive) + ".json"), receipt)
    write_json(evidence / f"prepare-{os.environ['SLURM_JOB_ID']}.json", receipt)
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
