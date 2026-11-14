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
from lean_corpus.deduplicate import deduplicate
from lean_corpus.export import export
from lean_corpus.extraction_cache import implementation_digest
from lean_corpus.lean import inspect
from lean_corpus.normalize import normalize
from lean_corpus.runtime import RuntimeGuard
from lean_corpus.verify import verify
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
    bounds = parser.add_mutually_exclusive_group()
    bounds.add_argument("--limit", type=int)
    bounds.add_argument("--selection", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", args.probe_import):
        parser.error("probe import must be a module name")
    if args.expected_input_rows <= 0 or args.timeout <= 0:
        parser.error("expected rows and timeout must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("limit must be positive")
    state_file = args.work_dir.parent / "full-run-state.json"
    state = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "code_digest": implementation_digest(),
        "sources": args.source,
        "dataset_revision": args.dataset_revision,
        "expected_input_rows": args.expected_input_rows,
        "stages": {},
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
        for path in runtimes:
            environment, runtime = guard.get(path.stem)
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
        record("normalize")
        result = normalize(
            args.work_dir, args.sources, args.source, args.limit, args.selection
        )
        extraction = read_json(args.work_dir / "reports/extraction.json")
        if extraction["input_occurrences"] != args.expected_input_rows:
            raise RuntimeError("normalization did not visit the expected input rows")
        record("normalized", result)
        record("deduplicated", deduplicate(args.work_dir))
        record("verify")
        record("verified", verify(args.work_dir, timeout=args.timeout))
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
