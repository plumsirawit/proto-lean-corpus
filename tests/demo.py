"""Bounded synthetic demo, not upstream acquisition or publication."""

import argparse
from pathlib import Path
from lean_corpus.acquire import acquire, check_snapshot
from lean_corpus.common import write_json
from lean_corpus.normalize import normalize
from lean_corpus.deduplicate import deduplicate
from lean_corpus.verify import verify
from lean_corpus.export import export


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    sources = "configs/sources.yaml"
    sid = acquire(
        args.work_dir, sources, "numina_math_lean", Path("tests/fixtures/numina")
    )
    manifest = check_snapshot(args.work_dir / "snapshots" / sid)
    write_json(
        args.work_dir / "runtimes" / f"{manifest['environment_id']}.json",
        {"project_dir": str(Path("tests/fixtures/numina").resolve()), "lean_path": []},
    )
    print("normalize:", normalize(args.work_dir, sources))
    print("deduplicate:", deduplicate(args.work_dir))
    print("verify:", verify(args.work_dir))
    print("export:", export(args.work_dir, args.output_dir))


if __name__ == "__main__":
    main()
