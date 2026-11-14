"""Process already acquired inputs locally; never acquires or publishes datasets."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import signal
import subprocess
import traceback
from lean_corpus.acquire import selected_snapshots
from lean_corpus.common import read_json, write_json
from lean_corpus.deduplicate import deduplicate, require_current
from lean_corpus.export import export
from lean_corpus.extraction_cache import implementation_digest
from lean_corpus.lean import POLICY, inspect
from lean_corpus.normalize import normalize
from lean_corpus.runtime import RuntimeGuard
from lean_corpus.verify import verify
from lean_corpus.parallel_verify import verify_parallel
from lean_corpus.worker import STATS, close_all


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--sources", default="configs/sources.yaml")
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--expected-input-rows", type=int, required=True)
    parser.add_argument("--probe-import", default="Mathlib")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--resume-verification",
        action="store_true",
        help="reuse completed normalized/canonical inputs from this checkpoint",
    )
    bounds = parser.add_mutually_exclusive_group()
    bounds.add_argument("--limit", type=int)
    bounds.add_argument("--selection", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", args.probe_import):
        parser.error("probe import must be a module name")
    if args.expected_input_rows <= 0 or args.timeout <= 0 or args.workers <= 0:
        parser.error("expected rows and timeout must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("limit must be positive")
    state_file = args.work_dir.parent / "full-run-state.json"
    previous = read_json(state_file) if state_file.exists() else {}
    if args.resume_verification:
        if args.limit is not None or args.selection:
            parser.error("verification resume cannot change the input selection")
        if (
            previous.get("sources") != args.source
            or previous.get("dataset_revision") != args.dataset_revision
            or previous.get("expected_input_rows") != args.expected_input_rows
            or previous.get("limit") is not None
            or previous.get("selection_file") is not None
            or not {"normalized", "deduplicated"} <= previous.get("stages", {}).keys()
        ):
            raise ValueError(
                "checkpoint has no matching completed full normalization/deduplication"
            )
        require_current(args.work_dir)
    state = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "code_digest": implementation_digest(),
        "sources": args.source,
        "dataset_revision": args.dataset_revision,
        "expected_input_rows": args.expected_input_rows,
        "limit": args.limit,
        "selection_file": args.selection.name if args.selection else None,
        "stages": {},
    }
    if args.resume_verification:
        state["resumed_from"] = previous
        state["stages"] = {
            k: previous["stages"][k] for k in ("normalized", "deduplicated")
        }

    def record(phase, result=None):
        state.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat())
        if result is not None:
            state["stages"][phase] = result
        state["worker_stats"] = dict(STATS)
        write_json(state_file, state)
        print(phase, result if result is not None else "", flush=True)

    def interrupted(signum, frame):
        raise InterruptedError(
            f"processing interrupted by signal {signum}; resume from completed caches"
        )

    signal.signal(signal.SIGTERM, interrupted)
    try:
        record("preflight")
        snapshots = list(selected_snapshots(args.work_dir, args.source))
        if not snapshots or any(
            m["revision"] != args.dataset_revision for _, m in snapshots
        ):
            raise RuntimeError("missing snapshot or unexpected pinned dataset revision")
        guard = RuntimeGuard(args.work_dir)
        runtimes = list((args.work_dir / "runtimes").glob("*.json"))
        if not runtimes:
            raise RuntimeError("a prepared checking runtime is required")
        effective = {}
        for path in runtimes:
            environment, runtime = guard.get(path.stem)
            paths = runtime.get("lean_path", [])
            if any(Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
                raise RuntimeError(
                    "the batch template requires project-relative module paths"
                )
            effective[path.stem] = {
                "runner": runtime.get("runner", "process"),
                "memory_mb": runtime.get("memory_mb", 4096),
                "parse_timeout_seconds": runtime.get("parse_timeout", 60),
                "worker_max_cases": runtime.get("worker_max_cases", 512),
                "lean_path": paths,
            }
            probe = inspect(
                f"import {args.probe_import}\ntheorem __corpus_runtime_probe : True := by trivial",
                environment,
                runtime,
                "verify",
                "__corpus_runtime_probe",
                args.timeout,
            )
            if probe["errors"] or not probe.get("is_theorem") or probe.get("axioms"):
                raise RuntimeError(
                    "runtime preflight did not pass the real kernel check"
                )
        guard.finish()
        execution = {
            "code_revision": state["code_revision"],
            "code_digest": state["code_digest"],
            "verification_timeout_seconds": args.timeout,
            "verification_workers": args.workers,
            "reused_normalization": args.resume_verification,
            "environments": effective,
            "verification_policy": POLICY,
            "task_options": "original task options and Lean defaults are unchanged",
        }
        write_json(args.work_dir / "reports/effective-execution.json", execution)
        state["effective_execution"] = execution
        if not args.resume_verification:
            record("normalize")
            result = normalize(
                args.work_dir, args.sources, args.source, args.limit, args.selection
            )
            record("normalized", result)
        extraction = read_json(args.work_dir / "reports/extraction.json")
        if extraction["input_occurrences"] != args.expected_input_rows:
            raise RuntimeError("normalization did not visit the expected input rows")
        if not args.resume_verification:
            record("deduplicated", deduplicate(args.work_dir))
        close_all()
        record("verify")
        result = (
            verify(args.work_dir, timeout=args.timeout)
            if args.workers == 1
            else verify_parallel(args.work_dir, args.workers, timeout=args.timeout)
        )
        record("verified", result)
        record("export")
        record("complete", export(args.work_dir, args.output_dir, args.sources))
    except BaseException as error:
        state["error"] = str(error)
        state["traceback"] = traceback.format_exc()
        record(
            "interrupted"
            if isinstance(error, (InterruptedError, KeyboardInterrupt))
            else "failed"
        )
        raise
    finally:
        close_all()


if __name__ == "__main__":
    main()
