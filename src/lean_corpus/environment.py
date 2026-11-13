"""Explicitly resolve a snapshot's checking environment without mutating originals."""

from pathlib import Path
import shutil
from .acquire import check_snapshot, inventory, project_environment
from .common import identity, read_json, write_json


def resolve_environment(work, snapshot_id, project, lean_path=None):
    work, project = Path(work), Path(project).resolve()
    if work.resolve().is_relative_to(project):
        raise ValueError(
            "work directory must not be inside the supplied environment project"
        )
    manifest = check_snapshot(work / "snapshots" / snapshot_id)
    env = project_environment(project, manifest["source_key"])
    if not env["resolved"]:
        raise ValueError(
            "project environment is unresolved: supply lean-toolchain and a fully pinned lake-manifest.json"
        )
    eid = identity("environment", env)
    # Preserve reconstructible project inputs as artifacts; never copy .lake/packages.
    artifact = work / "environment-projects" / eid
    files = inventory(project)
    if artifact.exists():
        if inventory(artifact) != files:
            raise ValueError("environment project integrity failure")
    else:
        artifact.mkdir(parents=True)
        for entry in files:
            dest = artifact / entry["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project / entry["path"], dest)
    write_json(work / "environments" / f"{eid}.json", env)
    write_json(
        work / "runtimes" / f"{eid}.json",
        {"project_dir": str(project), "lean_path": lean_path or []},
    )
    resolution = {
        "snapshot_id": snapshot_id,
        "previous_environment_id": manifest["environment_id"],
        "environment_id": eid,
        "transformation": "Explicit checking-environment resolution; renormalization changes task/proof IDs",
    }
    write_json(work / "resolutions" / f"{snapshot_id}.json", resolution)
    return resolution


def environment_for(work, snapshot_id, manifest):
    resolution = Path(work) / "resolutions" / f"{snapshot_id}.json"
    if resolution.exists():
        return read_json(resolution)["environment_id"]
    return manifest["environment_id"]
