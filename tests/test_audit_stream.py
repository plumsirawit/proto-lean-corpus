import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from lean_corpus.audit_stream import VerificationIndex, report_items
from lean_corpus.common import file_hash


def test_stream_boundaries_unicode_and_numbers(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"input_occurrences": 1234567890, "reports": [{"reason": "雪\\\"" * 50}, {"reason": "other"}], "empty": []}, ensure_ascii=False, indent=2))
    for block in [1, 2, 7, 64, 65536]:
        items = list(report_items(report, arrays={"reports"}, block=block))
        assert items[0] == ("input_occurrences", None, 1234567890)
        assert [item[2] for item in items if item[0] == "reports"] == json.loads(report.read_text())["reports"]


@pytest.mark.parametrize("payload", ['{"reports":[1,]}', '{"reports":[1]', '{"reports":[]} trailing', '{"reports":[],"reports":[]}', '{}'])
def test_corrupt_or_missing_report_rejected(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        list(report_items(path, arrays={"reports"}, required={"reports"}, block=2))


def make_index(tmp_path, attempts, problems, **extra):
    report = tmp_path / "verification.json"
    report.write_text(json.dumps(dict(attempts=attempts, problems=problems, limit=None, policy={}, **extra)))
    return VerificationIndex(tmp_path / "index.sqlite", report)


def test_index_reconciles_once_and_rejects_leftovers(tmp_path):
    index = make_index(tmp_path, [{"proof_id": "p", "status": "invalid", "reason": "admission"}], {"t": {"status": "elaborated", "reason": "statement"}})
    assert index.pop("attempts", "p") == ("invalid", "admission")
    with pytest.raises(ValueError, match="Missing verification"):
        index.pop("attempts", "p")
    with pytest.raises(ValueError, match="independently match"):
        index.finish()
    assert index.pop("problems", "t") == ("elaborated", "statement")
    index.finish()


def test_duplicate_attempt_rejected(tmp_path):
    item = {"proof_id": "p", "status": "valid", "reason": "kernel"}
    with pytest.raises(ValueError, match="Duplicate verification"):
        make_index(tmp_path, [item, item], {})


def test_empty_results_and_existing_index(tmp_path):
    make_index(tmp_path, [], {}).finish()
    with pytest.raises(FileExistsError):
        make_index(tmp_path, [], {})


def test_partial_verification_rejected(tmp_path):
    report = tmp_path / "verification.json"
    report.write_text('{"attempts":[],"problems":{},"limit":1,"policy":{}}')
    with pytest.raises(ValueError, match="unlimited"):
        VerificationIndex(tmp_path / "index.sqlite", report)


@pytest.mark.parametrize("fault", [None, "coverage", "status", "extra_result", "missing_result"])
def test_archived_full_audit(tmp_path, fault):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source, job = "test-source", "123"
    root = tmp_path / "inputs" / source
    tables = {
        "problems": [{"id": "t", "target_name": "theorem_t", "source_refs": []}],
        "proofs": [{"id": "p", "problem_id": "t", "source_refs": [], "verification_status": "invalid"}],
        "provenance": [{"id": "origin"}],
    }
    for location in [root / "corpus/canonical", root / f"full-export-{job}"]:
        location.mkdir(parents=True)
        for name, rows in tables.items():
            pq.write_table(pa.Table.from_pylist(rows), location / f"{name}.parquet")
    reports = root / "corpus/reports"
    reports.mkdir()
    (reports / "extraction.json").write_text(json.dumps({"input_occurrences": 2 if fault == "coverage" else 1, "reports": [{"source_id": source, "location": "row=0", "reason": "unsupported syntax"}]}))
    attempts = [{"proof_id": "p", "status": "valid" if fault == "status" else "invalid", "reason": "admission"}]
    if fault == "extra_result":
        attempts.append(dict(attempts[0], proof_id="extra"))
    if fault == "missing_result":
        attempts = []
    (reports / "verification.json").write_text(json.dumps({"attempts": attempts, "problems": {"t": {"status": "elaborated", "reason": "statement"}}, "limit": None, "policy": {}}))
    archive = tmp_path / "archive.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(root, arcname=source)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"run_state": {"sources": [source], "expected_input_rows": 1, "code_revision": "test"}, "job_id": job, "archive": str(archive), "complete": True, "sha256": file_hash(archive)}))
    stage, work = tmp_path / "stage", tmp_path / "audit"
    stage.mkdir()
    work.mkdir()
    script = Path(__file__).resolve().parents[1] / "scripts/audit_local_export.py"
    result = subprocess.run([sys.executable, str(script), str(stage), str(receipt), str(work)], capture_output=True, text=True, timeout=30)
    audit = stage / f"full-audit-{job}.json"
    if fault:
        assert result.returncode != 0
        assert not audit.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(audit.read_text())["counts"] == {"problems": 1, "proofs": 1, "provenance": 1}
        failures = (stage / f"failures-{source}-full-{job}.md").read_text()
        assert "admission" in failures and "unsupported syntax" in failures
