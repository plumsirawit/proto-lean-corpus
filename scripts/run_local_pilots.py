"""Run one bounded, already acquired real-data pilot; never fetch or publish data."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time
import traceback

from lean_corpus.acquire import selected_snapshots
from lean_corpus.common import file_hash, read_json, write_json
from lean_corpus.deduplicate import deduplicate
from lean_corpus.export import export
from lean_corpus.extraction_cache import implementation_digest
from lean_corpus.lean import POLICY, inspect
from lean_corpus.normalize import normalize
from lean_corpus.parallel_extract import PARALLEL_RECORD_SOURCES, warm_record_extraction
from lean_corpus.parallel_verify import verify_parallel
from lean_corpus.runtime import RuntimeGuard
from lean_corpus.schema import read_tables
from lean_corpus.shutdown import install_shutdown_handler
from lean_corpus.verify import verify
from lean_corpus.worker import STATS, close_all

SOURCES = ("goedel_pset_v1", "lean_workbook", "putnam_bench")
OPERATIONAL_REASONS = {
    "unresolved_environment",
    "toolchain_unavailable",
    "project_unavailable",
    "environment_failure",
    "imports_unavailable",
    "isolation_unavailable",
    "infrastructure_failure",
    "parser_or_import_failure",
    "inspection_protocol_failure",
    "checker_startup_or_process_failure",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--size", type=int, choices=(10, 100, 1000), required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--extraction-workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.workers <= 0 or args.timeout <= 0:
        parser.error("workers and timeout must be positive")
    if args.extraction_workers < 1 or (
        args.extraction_workers > 1
        and (args.source not in PARALLEL_RECORD_SOURCES or args.selection is None)
    ):
        parser.error("parallel extraction requires a selected record-source pilot")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.output_dir / "pilot-result.json"
    state = {
        "complete": False,
        "source": args.source,
        "requested_occurrences": args.size,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "code_digest": implementation_digest(),
        "policy": POLICY,
        "timeout_seconds": args.timeout,
        "workers": args.workers,
    }

    def record(phase):
        state["phase"] = phase
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(result_path, state)
        print(phase, flush=True)

    install_shutdown_handler()
    try:
        record("preflight")
        snapshots = list(selected_snapshots(args.work_dir, [args.source]))
        if len(snapshots) != 1:
            raise ValueError("a pilot requires exactly one acquired source snapshot")
        state["snapshot_id"] = snapshots[0][0].name
        state["dataset_revision"] = snapshots[0][1]["revision"]
        state["upstream_revision_label"] = snapshots[0][1].get("revision_label")
        guard = RuntimeGuard(args.work_dir)
        runtimes = list((args.work_dir / "runtimes").glob("*.json"))
        if len(runtimes) != 1:
            raise ValueError("a pilot requires one explicitly prepared runtime")
        env, runtime = guard.get(runtimes[0].stem)
        state["memory_mb"] = runtime.get("memory_mb", 4096)
        state["parse_timeout_seconds"] = runtime.get("parse_timeout", 60)
        state["runner"] = runtime.get("runner", "process")
        probe = inspect(
            "import Mathlib\ntheorem __pilot_probe : True := by trivial",
            env,
            runtime,
            "verify",
            "__pilot_probe",
            args.timeout,
        )
        if probe["errors"] or not probe.get("is_theorem") or probe.get("axioms"):
            raise RuntimeError("the real Mathlib/kernel preflight failed")
        guard.finish()
        state["environment_id"] = runtimes[0].stem
        state["toolchain"] = env["toolchain"]
        if args.extraction_workers > 1:
            close_all()
            record("warm_extraction_cache")
            started = time.monotonic()
            state["extraction_warmup"] = warm_record_extraction(
                args.work_dir, args.source, args.extraction_workers, args.selection
            )
            state["extraction_warmup"]["seconds"] = time.monotonic() - started
        record("normalize")
        started = time.monotonic()
        bounds = (
            {"selection": args.selection} if args.selection else {"limit": args.size}
        )
        state["normalization"] = normalize(
            args.work_dir, "configs/sources.yaml", [args.source], **bounds
        )
        extraction = read_json(args.work_dir / "reports/extraction.json")
        seen = extraction["input_occurrences"]
        if args.extraction_workers > 1:
            state["first_normalization_cache_hits"] = extraction["cache_hits"]
            if (
                extraction["cache_hits"] + state["extraction_warmup"]["deferred_rows"]
                != seen
            ):
                raise RuntimeError("parallel extraction cache did not cover the pilot")
        if seen != args.size and not (
            args.source == "putnam_bench"
            and args.size == 1000
            and 0 < seen < args.size
            and not args.selection
        ):
            raise RuntimeError(f"expected {args.size} selected occurrences, got {seen}")
        state["visited_occurrences"] = seen
        state["source_exhausted_before_cap"] = seen < args.size
        before = {
            p.name: file_hash(p)
            for p in (args.work_dir / "normalized").glob("*.parquet")
        }
        normalize(args.work_dir, "configs/sources.yaml", [args.source], **bounds)
        if before != {
            p.name: file_hash(p)
            for p in (args.work_dir / "normalized").glob("*.parquet")
        }:
            raise RuntimeError("normalization is not byte-repeatable")
        state["repeat_normalization_equal"] = True
        state["normalization_seconds"] = time.monotonic() - started
        state["deduplicated"] = deduplicate(args.work_dir)
        record("verify")
        started = time.monotonic()
        state["verification"] = (
            verify(args.work_dir, timeout=args.timeout)
            if args.workers == 1
            else verify_parallel(args.work_dir, args.workers, timeout=args.timeout)
        )
        report = read_json(args.work_dir / "reports/verification.json")
        state["verification_seconds"] = time.monotonic() - started
        tables = read_tables(args.work_dir / "canonical")
        proofs = {p["id"]: p for p in tables["proofs"]}
        problems = {p["id"]: p for p in tables["problems"]}
        failures = {
            "source": {
                key: state[key]
                for key in (
                    "source",
                    "snapshot_id",
                    "dataset_revision",
                    "upstream_revision_label",
                    "environment_id",
                    "toolchain",
                )
            },
            "proofs": [
                {
                    **r,
                    "problem_id": proofs[r["proof_id"]]["problem_id"],
                    "target_name": problems[proofs[r["proof_id"]]["problem_id"]][
                        "target_name"
                    ],
                    "source_refs": proofs[r["proof_id"]]["source_refs"],
                }
                for r in report["attempts"]
                if r["status"] != "valid"
            ],
            "statements": [
                {
                    **r,
                    "problem_id": pid,
                    "target_name": problems[pid]["target_name"],
                    "source_refs": problems[pid]["source_refs"],
                }
                for pid, r in report["problems"].items()
                if r["status"] != "elaborated"
            ],
            "extraction": extraction["reports"],
        }
        write_json(args.output_dir / "failures.json", failures)
        state["proof_reasons"] = dict(
            Counter(f"{r['status']}:{r['reason']}" for r in report["attempts"])
        )
        state["statement_reasons"] = dict(
            Counter(f"{r['status']}:{r['reason']}" for r in report["problems"].values())
        )
        state["extraction_reasons"] = dict(
            Counter(r["reason"] for r in extraction["reports"])
        )
        operational = [
            r
            for group in (failures["proofs"], failures["statements"])
            for r in group
            if r["reason"].removeprefix("problem_") in OPERATIONAL_REASONS
        ]
        if operational:
            state["operational_failures"] = len(operational)
            raise RuntimeError(
                "unresolved operational failures; next pilot gate stopped"
            )
        record("export")
        state["export"] = export(args.work_dir, args.output_dir / "export")
        state["worker_stats"] = dict(STATS)
        state["complete"] = True
        record("complete")
        print(state, flush=True)
    except BaseException as error:
        state["error"] = str(error)
        state["traceback"] = traceback.format_exc()
        record("failed")
        raise
    finally:
        close_all()


if __name__ == "__main__":
    main()
