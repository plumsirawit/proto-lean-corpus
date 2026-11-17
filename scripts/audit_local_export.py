"""Audit one final checkpoint and list failures with source references.

Run with the staging directory, final checkpoint receipt, and job-local scratch
directory as positional arguments. The audit writes its receipt and inventory
to the staging directory.
"""

from collections import Counter
from itertools import zip_longest
import json
from pathlib import Path
import shutil
import sys
import tarfile

import pyarrow.parquet as pq

from lean_corpus.common import file_hash, write_json
from lean_corpus.audit_stream import VerificationIndex, report_items

stage, receipt_path, audit_work = map(Path, sys.argv[1:])
receipt = json.loads(receipt_path.read_text())
(source,) = receipt["run_state"]["sources"]
job = receipt["job_id"]
archive = Path(receipt["archive"])
if not receipt["complete"] or file_hash(archive) != receipt["sha256"]:
    raise ValueError("The final archive is incomplete or its checksum did not match")

table_names = ("problems", "proofs", "provenance")
canonical = {name: audit_work / f"canonical-{name}.parquet" for name in table_names}
exported = {name: audit_work / f"exported-{name}.parquet" for name in table_names}
members = {
    f"{source}/corpus/canonical/{name}.parquet": canonical[name] for name in table_names
}
members.update(
    {
        f"{source}/full-export-{job}/{name}.parquet": exported[name]
        for name in table_names
    }
)
extraction_path = audit_work / "extraction.json"
verification_path = audit_work / "verification.json"
members[f"{source}/corpus/reports/extraction.json"] = extraction_path
members[f"{source}/corpus/reports/verification.json"] = verification_path

with tarfile.open(archive, "r|", bufsize=1024 * 1024) as stream:
    for member in stream:
        if (path := members.pop(member.name, None)) is not None:
            if not member.isfile():
                raise ValueError(f"Expected a file at {member.name}")
            with stream.extractfile(member) as contents, path.open("wb") as output:
                shutil.copyfileobj(contents, output)
        if not members:
            break
if members:
    raise ValueError(f"Missing archived files: {list(members)}")


def batches(path, columns=None):
    return pq.ParquetFile(path).iter_batches(
        batch_size=4096, columns=columns, use_threads=False
    )


counts = {}
for name in table_names:
    total = 0
    for original, copy in zip_longest(
        batches(canonical[name]), batches(exported[name])
    ):
        if original is None or copy is None or not original.equals(copy):
            raise ValueError(f"Canonical and export differ for {name}")
        total += original.num_rows
    counts[name] = total


def inline(text):
    return str(text).replace("\n", " ").replace("`", "'")


def relpath(refs):
    return ", ".join(
        " ".join(
            str(ref[key])
            for key in ("source_id", "upstream_id", "location")
            if ref.get(key)
        )
        for ref in refs
    )


index = VerificationIndex(audit_work / "verification-index.sqlite", verification_path)
seen = None
reasons = {"extraction": Counter(), "proofs": Counter(), "statements": Counter()}
out = stage / f"failures-{source}-full-{job}.md"
temporary = out.with_suffix(".tmp")
with temporary.open("w") as report:
    report.write(
        f"# {source} full-run failure inventory\n\n"
        f"Slurm job {job}. Archive `{archive.name}`. Code "
        f"`{receipt['run_state']['code_revision']}`.\n\n"
        f"## Extraction\n\n"
    )
    for field, _, item in report_items(
        extraction_path, arrays={"reports"}, required={"reports", "input_occurrences"}
    ):
        if field == "input_occurrences":
            seen = item
        if field != "reports":
            continue
        reason = item["reason"]
        reasons["extraction"][reason] += 1
        report.write(
            f"- `{inline(item['source_id'])} {inline(item['location'])}` — {inline(reason)}\n"
        )

    report.write("\n## Statements\n\n")
    for batch in batches(canonical["problems"], ["id", "target_name", "source_refs"]):
        for problem in batch.to_pylist():
            status, detail = index.pop("problems", problem["id"])
            reason = f"{status}:{detail}"
            reasons["statements"][reason] += 1
            if status != "elaborated":
                report.write(
                    f"- `{problem['id']}` (`{inline(problem['target_name'])}`) at "
                    f"`{inline(relpath(problem['source_refs']))}` — {inline(reason)}\n"
                )

    report.write("\n## Proofs\n\n")
    columns = ["id", "problem_id", "source_refs", "verification_status"]
    for batch in batches(canonical["proofs"], columns):
        for proof in batch.to_pylist():
            status, detail = index.pop("attempts", proof["id"])
            if status != proof["verification_status"]:
                raise ValueError(f"Proof status differs for {proof['id']}")
            reason = f"{status}:{detail}"
            reasons["proofs"][reason] += 1
            if status != "valid":
                report.write(
                    f"- `{proof['id']}` for `{proof['problem_id']}` at "
                    f"`{inline(relpath(proof['source_refs']))}` — {inline(reason)}\n"
                )

index.finish()
if seen != receipt["run_state"]["expected_input_rows"]:
    raise ValueError("The normalized input count differed from the independent source count")
temporary.replace(out)
write_json(
    stage / f"full-audit-{job}.json",
    {
        "complete": True,
        "job_id": job,
        "source": source,
        "archive": str(archive),
        "sha256": receipt["sha256"],
        "counts": counts,
        "input_occurrences": seen,
        "reasons": reasons,
        "failures_markdown": out.name,
    },
)
print(
    f"Verified export parity, coverage, and SHA-256 for {source}, job {job}", flush=True
)
