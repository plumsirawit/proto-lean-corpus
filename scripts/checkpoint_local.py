"""Archive node-local work with checksums, quota margin, and two-generation retention."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import errno
import os
from pathlib import Path
import subprocess
from euler_quota import check


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--phase", choices=["periodic", "final"], required=True)
    parser.add_argument("--runner-exit", type=int)
    args = parser.parse_args()
    if not args.job_id.isdigit() or not args.workspace.is_dir():
        parser.error("numeric job ID and existing workspace required")
    if args.stage.resolve().is_relative_to(args.workspace.resolve()):
        parser.error("checkpoint destination must be outside the workspace")

    def walk_error(error):
        if error.errno != errno.ENOENT:
            raise error

    estimate = 0
    for directory, subdirs, files in os.walk(
        args.workspace, onerror=walk_error, followlinks=False
    ):
        for name in subdirs + files:
            try:
                estimate += (Path(directory) / name).lstat().st_size + 4096
            except FileNotFoundError:
                # Atomic stage replacement can remove a path between listing and stat.
                continue
    check(args.stage, estimate, 20)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    stem = f"full-{args.job_id}"
    archive = args.stage / f"{stem}-{stamp}.tar"
    temporary = archive.with_suffix(".tar.partial")
    result = subprocess.run(
        [
            "tar",
            "--exclude=*.tmp",
            f"--exclude={args.workspace.name}/tmp????????",
            "-cf",
            str(temporary),
            "-C",
            str(args.workspace.parent),
            args.workspace.name,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("checkpoint archive failed: " + result.stderr)
    if args.phase == "final" and args.runner_exit == 0 and result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "completed run changed during final archival: " + result.stderr
        )
    h = hashlib.sha256()
    with temporary.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    temporary.replace(archive)
    # Verify the saved replacement before updating latest or pruning generations.
    saved = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            saved.update(chunk)
    if saved.hexdigest() != h.hexdigest():
        raise RuntimeError("saved checkpoint checksum mismatch")
    checksum = archive.with_suffix(".tar.sha256")
    checksum.write_text(f"{h.hexdigest()}  {archive}\n")
    run_state = args.workspace / "full-run-state.json"
    state = json.loads(run_state.read_text()) if run_state.exists() else {}
    complete = (
        args.phase == "final"
        and args.runner_exit == 0
        and state.get("phase") == "complete"
    )
    manifest = {
        "job_id": args.job_id,
        "archive": str(archive),
        "sha256": h.hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase": args.phase,
        "runner_exit": args.runner_exit,
        "complete": complete,
        "resume_only": not complete,
        "run_state": state,
        "tar_exit": result.returncode,
        "tar_diagnostics": result.stderr,
        "cache_files": {
            name: sum(
                p.is_file() for p in (args.workspace / "corpus" / name).glob("*.json")
            )
            for name in ("extraction-cache", "statement-cache", "verification-cache")
        },
    }
    atomic_json(archive.with_suffix(".tar.json"), manifest)
    atomic_json(args.stage / f"{stem}-latest.json", manifest)
    # Only prune this job's older, explicitly identified checkpoint copies.
    generations = sorted(args.stage.glob(f"{stem}-[0-9]*.tar"), reverse=True)
    for old in generations[2:]:
        metadata = old.with_suffix(".tar.json")
        if not metadata.exists():
            continue
        previous = json.loads(metadata.read_text())
        if previous.get("job_id") == args.job_id and previous.get("archive") == str(
            old
        ):
            old.unlink()
            old.with_suffix(".tar.sha256").unlink(missing_ok=True)
            metadata.unlink()
    print(
        json.dumps(
            {k: manifest[k] for k in ("archive", "sha256", "complete", "cache_files")}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
