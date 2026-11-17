"""Independent bounded pilot audit; hashes originals and reconciles all attempts.

Processing receipts are authoritative only after archive, identity, table, source,
and complete failure-inventory checks succeed. This script does not run processing.
"""

from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import io
import json
import os
import sys
import tarfile
import hashlib
import pyarrow.parquet as pq
from lean_corpus.common import digest, file_hash, identity, text, write_json
from euler_quota import check
from lean_corpus.schema import validate

stage = Path(sys.argv[1])
size = int(sys.argv[2])
results = []
check(stage, 128 * 1024**2, 10)
for receipt in sys.argv[3:]:
    m = json.loads(Path(receipt).read_text())
    key = m["source"]
    job = int(m["archive"].rsplit("-", 1)[-1].split(".", 1)[0])
    assert m["complete"] and m["runner_exit"] == 0
    assert m["size"] == size
    seen = m["pilot"]["visited_occurrences"]
    assert seen == size or (
        key in {"putnam_bench", "goedel_prover_v2_sft", "nemotron_math_proofs_v1", "mathlib4", "tau_ceti"}
        and 0 < seen < size
        and m["pilot"]["source_exhausted_before_cap"]
        and (key == "putnam_bench" or m["pilot"].get("source_occurrences") == seen)
    )
    assert m["pilot"]["repeat_normalization_equal"]
    checksum = file_hash(Path(m["archive"]))
    assert (
        checksum == m["sha256"] == Path(m["archive"] + ".sha256").read_text().split()[0]
    )
    tables = {}
    exported = {}
    manifests = {}
    raw_hashes = {}
    verification = None
    failures = None
    quality = None
    selection = None
    with tarfile.open(m["archive"], "r|", bufsize=1024 * 1024) as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = member.name
            if name == f"{key}/selection-{size}.json":
                selection = json.load(archive.extractfile(member))
            elif name.startswith(key + "/corpus/canonical/") and name.endswith(
                ".parquet"
            ):
                tables[Path(name).stem] = pq.read_table(
                    io.BytesIO(archive.extractfile(member).read())
                ).to_pylist()
            elif (
                name.startswith(f"{key}/results/pilot-{size}/export/")
                and name.count("/") == 4
                and name.endswith(".parquet")
            ):
                exported[Path(name).stem] = pq.read_table(
                    io.BytesIO(archive.extractfile(member).read())
                ).to_pylist()
            elif name == key + "/corpus/reports/verification.json":
                verification = json.load(archive.extractfile(member))
            elif name == key + "/corpus/reports/extraction.json":
                extraction = json.load(archive.extractfile(member))
            elif name == f"{key}/results/pilot-{size}/failures.json":
                failures = json.load(archive.extractfile(member))
            elif name == f"{key}/results/pilot-{size}/export/quality.json":
                quality = json.load(archive.extractfile(member))
            elif name.startswith(key + "/corpus/snapshots/"):
                tail = name[len(key + "/corpus/snapshots/") :]
                sid, relative = tail.split("/", 1)
                if relative == "manifest.json":
                    manifests[sid] = json.load(archive.extractfile(member))
                elif relative.startswith("original/"):
                    h = hashlib.sha256()
                    f = archive.extractfile(member)
                    while chunk := f.read(1024 * 1024):
                        h.update(chunk)
                    raw_hashes[(sid, relative[len("original/") :])] = h.hexdigest()
    validate(tables)
    assert tables == exported
    assert quality["counts"] == {k: len(v) for k, v in tables.items()}
    assert extraction["input_occurrences"] == seen
    if key in {"goedel_prover_v2_sft", "nemotron_math_proofs_v1", "mathlib4", "tau_ceti"}:
        assert selection is not None and extraction["selection"] == selection
        assert sum(len(indices) for files in selection["snapshots"].values() for indices in files.values()) == seen
        assert set(selection["snapshots"]) == set(manifests)
    for sid, manifest in manifests.items():
        assert identity("source", manifest) == sid
        for entry in manifest["files"]:
            assert raw_hashes[(sid, entry["path"])] == entry["sha256"]
    for provenance in tables["provenance"]:
        assert provenance["manifest_hash"] == digest(manifests[provenance["id"]])
    problems = {p["id"]: p for p in tables["problems"]}
    proofs = {p["id"]: p for p in tables["proofs"]}
    for p in problems.values():
        assert p["id"] == identity(
            "problem",
            {
                "source": text(p["formal_statement"]),
                "target": p["target_name"],
                "environment": p["environment_id"],
            },
        )
    for p in proofs.values():
        assert p["id"] == identity(
            "proof", {"problem": p["problem_id"], "term": text(p["formal_proof"])}
        )
    for group in (tables["problems"], tables["proofs"]):
        for row in group:
            for ref in row["source_refs"]:
                assert (ref["source_id"], ref["location"].split("#")[0]) in raw_hashes
    assert set(verification["problems"]) == set(problems)
    assert {r["proof_id"] for r in verification["attempts"]} == set(proofs)
    for r in verification["attempts"]:
        assert r["status"] == proofs[r["proof_id"]]["verification_status"]
    assert {r["proof_id"] for r in failures["proofs"]} == {
        pid for pid, p in proofs.items() if p["verification_status"] != "valid"
    }
    assert {r["problem_id"] for r in failures["statements"]} == {
        pid
        for pid, r in verification["problems"].items()
        if r["status"] != "elaborated"
    }
    assert failures["extraction"] == extraction["reports"]
    result = {
        "source": key,
        "job_id": job,
        "archive": m["archive"],
        "sha256": checksum,
        "upstream_revision": m["pilot"]["upstream_revision_label"],
        "snapshot_id": m["pilot"]["snapshot_id"],
        "code_revision": m["code_revision"],
        "counts": quality["counts"],
        "visited_occurrences": seen,
        "proof_statuses": dict(
            Counter(p["verification_status"] for p in proofs.values())
        ),
        "statement_statuses": dict(
            Counter(r["status"] for r in verification["problems"].values())
        ),
        "extraction_diagnostics": len(extraction["reports"]),
        "failure_inventory_member": f"{key}/results/pilot-{size}/failures.json",
        "checks": [
            "independent_archive_sha256",
            "raw_file_sha256",
            "canonical_ids",
            "schema_and_foreign_keys",
            "source_location_coverage",
            "export_equals_canonical",
            "complete_verification_and_failure_inventories",
        ],
    }
    result["proof_reasons"] = m["pilot"]["proof_reasons"]
    result["statement_reasons"] = m["pilot"]["statement_reasons"]
    result["extraction_reasons"] = m["pilot"]["extraction_reasons"]
    result["worker_stats"] = m["pilot"]["worker_stats"]
    (stage / f"failures-{key}-{size}-{job}.json").write_text(
        json.dumps(failures, ensure_ascii=False) + "\n"
    )
    results.append(result)
    print(json.dumps(result), flush=True)
report = {
    "complete": True,
    "at": datetime.now(timezone.utc).isoformat(),
    "size": size,
    "pilots": results,
}
dest = stage / f"audit-{size}-{os.environ['SLURM_JOB_ID']}.json"
write_json(dest, report)
if os.environ.get("LC_RECEIPT_PATH"):
    assert len(results) == 1
    write_json(Path(os.environ["LC_RECEIPT_PATH"]), {
        **report, "archive": results[0]["archive"], "sha256": results[0]["sha256"],
        "audit_job_id": os.environ["SLURM_JOB_ID"],
    })
print("AUDIT_COMPLETE", dest, flush=True)
