"""Preserve the complete pinned Nemotron JSONL and bind its own built environment.

Run only inside Slurm. Original physical-line selections include malformed or
informal-only records; no acceptance or extraction policy is changed here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

from euler_quota import check
from lean_corpus.acquire import acquire
from lean_corpus.common import file_hash, write_json
from lean_corpus.lean import inspect
from lean_corpus.worker import close_all
from lean_corpus.runtime import RuntimeGuard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()
    assert os.environ.get("SLURM_JOB_ID") and os.environ.get("TMPDIR")
    source = "nemotron_math_proofs_v1"
    pin = json.loads((args.evidence / f"{source}-pin.json").read_text())
    assert pin["revision"] == "97229c590831adfe96202f5cd071d444d535bf91"
    plan = json.loads((args.evidence / "nemotron-selection-plan.json").read_text())
    inventory = json.loads(
        (args.evidence / "nemotron-inventory-21198255.json").read_text()
    )
    assert inventory["complete"] and inventory["upstream_revision"] == pin["revision"]
    assert plan["source"] == source and plan["revision"] == pin["revision"]
    entries = [
        x
        for x in json.loads((args.evidence / f"{source}-sizes.json").read_text())
        if x["type"] == "file"
    ]
    assert {
        p.relative_to(args.raw).as_posix() for p in args.raw.rglob("*") if p.is_file()
    } == set(pin["files"])
    total = sum(x["size"] for x in entries)
    check(args.evidence, 8 * 1024**2, 12)
    check(args.stage, total + 256 * 1024**2, 8)
    hashes = {}
    for entry in entries:
        path = args.raw / entry["path"]
        assert not path.is_symlink() and path.stat().st_size == entry["size"]
        sha = hashlib.sha256()
        blob = hashlib.sha1(f"blob {entry['size']}\0".encode())
        with path.open("rb") as stream:
            while chunk := stream.read(1024**2):
                sha.update(chunk)
                if "lfs" not in entry:
                    blob.update(chunk)
        hashes[entry["path"]] = sha.hexdigest()
        assert sha.hexdigest() == inventory["sha256_files"][entry["path"]]
        assert (
            (sha.hexdigest() == entry["lfs"]["oid"])
            if "lfs" in entry
            else (blob.hexdigest() == entry["oid"])
        )
        print("VERIFIED_ORIGINAL", entry["path"], sha.hexdigest(), flush=True)
    workspace = Path(os.environ["TMPDIR"]) / source
    work = workspace / "corpus"
    acquire(
        work, "configs/sources.yaml", source, local=args.raw, revision=pin["revision"]
    )
    [snapshot] = (work / "snapshots").iterdir()
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert manifest["revision_label"] == pin["revision"]
    assert {x["path"]: x["sha256"] for x in manifest["files"]} == hashes
    previous = set()
    for size in (10, 100, 1000):
        selected = plan["selection_by_size"][str(size)]
        pairs = {(path, line) for path, lines in selected.items() for line in lines}
        assert previous <= pairs and len(pairs) == min(
            size, inventory["source_occurrences"]
        )
        assert all(
            path == "data/lean.jsonl" and 0 <= line < inventory["physical_jsonl_lines"]
            for path, line in pairs
        )
        write_json(
            workspace / f"selection-{size}.json",
            {"schema_version": "1", "snapshots": {snapshot.name: selected}},
        )
        previous = pairs
    write_json(
        workspace / "upstream-integrity.json",
        {
            "pin": pin,
            "sha256": hashes,
            "original_inventory": inventory,
            "selection_plan": plan,
        },
    )
    project = Path(os.environ["LC_LOCAL_PROJECT"])
    envpin = json.loads(
        (args.evidence / "download-nemotron-environment-state.json").read_text()
    )
    for dependency in envpin["repositories"]:
        root = (
            project
            if dependency["name"] == "mathlib"
            else project / ".lake/packages" / dependency["name"]
        )
        assert (
            subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            == dependency["revision"]
        )
        assert not subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        ).strip()
    for name in ("lean-toolchain", "lake-manifest.json", "lakefile.lean"):
        assert (project / name).read_bytes() == (
            args.evidence / ("nemotron-mathlib-" + name)
        ).read_bytes()
    os.environ.update(LC_LOCAL_WORKSPACE=str(workspace), LC_SOURCE=source)
    subprocess.run([os.sys.executable, "scripts/resolve_local_runtime.py"], check=True)
    guard = RuntimeGuard(work)
    [runtime] = (work / "runtimes").glob("*.json")
    environment, mapping = guard.get(runtime.stem)
    probe = inspect(
        "import Mathlib\ntheorem __nemotron_preparation_probe : True := by trivial",
        environment,
        mapping,
        "verify",
        "__nemotron_preparation_probe",
        180,
    )
    write_json(workspace / "environment-probe.json", probe)
    assert (
        not probe["errors"] and probe.get("is_theorem") and not probe.get("axioms")
    ), probe
    guard.finish()
    close_all()
    check(args.stage, total + 256 * 1024**2, 8)
    archive = args.stage / f"input-{source}-{os.environ['SLURM_JOB_ID']}.tar"
    assert not archive.exists()
    partial = archive.with_suffix(".tar.partial")
    with tarfile.open(partial, "w") as output:
        output.add(workspace, arcname=source)
    partial.replace(archive)
    sha = file_hash(archive)
    Path(str(archive) + ".sha256").write_text(f"{sha}  {archive}\n")
    assert file_hash(archive) == sha
    receipt = {
        "complete": True,
        "archive": str(archive),
        "sha256": sha,
        "job_id": os.environ["SLURM_JOB_ID"],
        "source": source,
        "snapshot_id": snapshot.name,
        "environment_id": runtime.stem,
        "source_occurrences": inventory["source_occurrences"],
        "upstream_revision": pin["revision"],
        "code_revision": os.environ["LC_CODE_REVISION"],
        "checks": [
            "complete_original_jsonl_retained",
            "original_upstream_lfs_and_blob_hashes",
            "nested_physical_line_selections",
            "own_exact_clean_dependency_locks",
            "real_kernel_probe",
            "archive_sha256_readback",
        ],
    }
    write_json(Path(str(archive) + ".json"), receipt)
    write_json(
        args.evidence / f"prepare-nemotron-{os.environ['SLURM_JOB_ID']}.json", receipt
    )
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
