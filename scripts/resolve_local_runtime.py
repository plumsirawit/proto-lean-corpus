"""Bind one acquired source to the prepared project inside a Slurm allocation."""

from pathlib import Path
import os

from lean_corpus.acquire import selected_snapshots
from lean_corpus.common import read_json, write_json
from lean_corpus.environment import resolve_environment


def main():
    work = Path(os.environ["LC_LOCAL_WORKSPACE"]) / "corpus"
    project = Path(os.environ["LC_LOCAL_PROJECT"])
    source = os.environ["LC_SOURCE"]
    [(snapshot, _)] = selected_snapshots(work, [source])
    roots = [project, *sorted((project / ".lake/packages").iterdir())]
    paths = [
        str(path.relative_to(project))
        for root in roots
        for relative in (".lake/build/lib/lean", ".lake/build/lib")
        if (path := root / relative).is_dir()
    ]
    resolution = resolve_environment(work, snapshot.name, project, paths)
    runtime_file = work / "runtimes" / (resolution["environment_id"] + ".json")
    runtime = read_json(runtime_file)
    runtime.update(
        runner="worker",
        memory_mb=int(os.environ.get("LC_CHECKER_MEMORY_MB", "8192")),
        worker_max_cases=int(os.environ.get("LC_WORKER_MAX_CASES", "128")),
        parse_timeout=180,
    )
    write_json(runtime_file, runtime)


if __name__ == "__main__":
    main()
