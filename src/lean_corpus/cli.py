import argparse
import json
from pathlib import Path
import sys
import subprocess
from .acquire import acquire, registry
from .common import read_json, write_json
from .deduplicate import deduplicate
from .export import export
from .environment import resolve_environment
from .normalize import normalize
from .lean import LeanFailure
from .verify import verify


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local proof-discovery corpus pipeline; never publishes data"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "acquire",
        "normalize",
        "deduplicate",
        "verify",
        "export",
        "runtime",
        "sources",
        "resolve-environment",
    ):
        p = commands.add_parser(name)
        if name != "sources":
            p.add_argument("--work-dir", type=Path, required=True)
        if name in {"acquire", "normalize", "export", "sources"}:
            p.add_argument("--sources", default="configs/sources.yaml")
        if name == "acquire":
            p.add_argument("--source", required=True)
            p.add_argument("--local", type=Path)
            p.add_argument("--revision")
            p.add_argument(
                "--include",
                action="append",
                help="HF download glob; repeatable; notices always included",
            )
        if name == "normalize":
            p.add_argument("--source", action="append")
            p.add_argument(
                "--selection",
                type=Path,
                help="explicit snapshot/file/row selection JSON",
            )
        if name in {"normalize", "verify"}:
            p.add_argument("--limit", type=int)
        if name == "verify":
            p.add_argument("--timeout", type=int, default=60)
            p.add_argument("--workers", type=int, default=1)
        if name == "export":
            p.add_argument("--output-dir", type=Path, required=True)
            p.add_argument("--pipeline", default="configs/pipeline.yaml")
        if name == "resolve-environment":
            p.add_argument("--snapshot-id", required=True)
            p.add_argument("--project-dir", type=Path, required=True)
            p.add_argument("--lean-path", action="append", default=[])
        if name == "runtime":
            p.add_argument("--runner", choices=["process", "worker"], default="process")
            p.add_argument("--worker-max-cases", type=int, default=512)
            p.add_argument("--memory-mb", type=int, default=4096)
            p.add_argument("--parse-timeout", type=int, default=60)
            p.add_argument("--environment-id", required=True)
            p.add_argument("--project-dir", type=Path, required=True)
            p.add_argument(
                "--lean-path",
                action="append",
                default=[],
                help="project-relative compiled module search path; repeatable",
            )
    args = parser.parse_args(argv)
    if getattr(args, "limit", None) is not None and args.limit < 0:
        parser.error("--limit must be nonnegative")
    if args.command == "runtime" and (
        args.memory_mb <= 0 or args.worker_max_cases <= 0 or args.parse_timeout <= 0
    ):
        parser.error("memory, timeout, and worker lifetime limits must be positive")
    try:
        if args.command == "sources":
            result = registry(args.sources)
        elif args.command == "acquire":
            result = acquire(
                args.work_dir,
                args.sources,
                args.source,
                args.local,
                args.revision,
                args.include,
            )
        elif args.command == "normalize":
            result = normalize(
                args.work_dir, args.sources, args.source, args.limit, args.selection
            )
        elif args.command == "deduplicate":
            result = deduplicate(args.work_dir)
        elif args.command == "verify":
            if args.workers == 1:
                result = verify(args.work_dir, args.limit, args.timeout)
            else:
                from .parallel_verify import verify_parallel

                result = verify_parallel(
                    args.work_dir, args.workers, args.limit, args.timeout
                )
        elif args.command == "export":
            result = export(args.work_dir, args.output_dir, args.sources, args.pipeline)
        elif args.command == "resolve-environment":
            result = resolve_environment(
                args.work_dir, args.snapshot_id, args.project_dir, args.lean_path
            )
        else:
            env = args.work_dir / "environments" / f"{args.environment_id}.json"
            read_json(env)
            result = {
                "project_dir": str(args.project_dir.resolve()),
                "lean_path": args.lean_path,
                "memory_mb": args.memory_mb,
                "parse_timeout": args.parse_timeout,
                "runner": args.runner,
                "worker_max_cases": args.worker_max_cases,
            }
            write_json(
                args.work_dir / "runtimes" / f"{args.environment_id}.json", result
            )
    except (
        ValueError,
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        LeanFailure,
    ) as e:
        print(f"lean-corpus: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
