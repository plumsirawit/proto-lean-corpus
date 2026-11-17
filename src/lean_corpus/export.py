from collections import Counter, defaultdict
from contextlib import ExitStack
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from . import __version__
from .acquire import check_snapshot
from .common import VERSION, digest, file_hash, read_json, safe_relative, write_json
from .deduplicate import require_current
from .schema import SCHEMAS, read_tables, write_tables
from .verify import cache_key
from .check_cache import CheckCacheCorruption, load_check
from .runtime import RuntimeGuard

DATA_LICENSE = "Our original contributions and compilation/database rights, to the extent applicable, are licensed under CC BY-SA 4.0. Third-party material retains its applicable upstream licenses, identified in the source manifest and accompanying notices. This license does not replace or restrict permissions independently granted by upstream licensors."


def _copy_cache_tree(source, destination):
    """Copy flat or nested cache directories without listing every entry in RAM."""
    destination.mkdir(parents=True, exist_ok=True)
    with os.scandir(source) as entries:
        for entry in entries:
            target = destination / entry.name
            if entry.is_dir():
                _copy_cache_tree(Path(entry.path), target)
            else:
                shutil.copy2(entry.path, target)


def export(
    work, output, sources="configs/sources.yaml", pipeline="configs/pipeline.yaml",
    *, spool_dir=None,
):
    """Export locally; optional exclusive job-local spools bound corpus memory.

    In spool mode the complete quality document stays in quality.json; the return
    value contains its counts/status summary and path, without loading ID lists.
    """
    with ExitStack() as resources:
        return _export(work, output, sources, pipeline, spool_dir, resources)


def _export(work, output, sources, pipeline, spool_dir, resources):
    work, output = Path(work), Path(output)
    require_current(work)
    if (work / "verification-incomplete.json").exists():
        raise ValueError("verification incomplete or failed: rerun verify")
    if output.exists():
        raise ValueError("export destination already exists; use a new directory")
    if spool_dir is None:
        tables = read_tables(work / "canonical")
        stale = []
        problems = {r["id"]: r for r in tables["problems"]}
        write_output = write_tables
        write_quality = write_json
        input_hashes = None
    else:
        from .disk_merge import parquet_rows
        from .disk_output import DiskMap, RowSpool, write_document, write_spooled_tables
        from .disk_schema import validate_parquet

        spool_dir = Path(spool_dir)
        spool_dir.mkdir()
        input_hashes = {name: file_hash(work / "canonical" / f"{name}.parquet")
                        for name in SCHEMAS}
        validate_parquet(work / "canonical", spool_dir / "input.sqlite")
        tables = {name: RowSpool(spool_dir / f"input-{name}.jsonl") for name in SCHEMAS}
        for name, row in parquet_rows(work / "canonical"):
            if name == "provenance":
                row = {**row, "acquired_at": row["acquired_at"].isoformat()}
            tables[name].append(row)
        problems = DiskMap(spool_dir / "problems.sqlite")
        resources.callback(problems.close)
        for row in tables["problems"]:
            problems[row["id"]] = row
        stale = RowSpool(spool_dir / "stale.jsonl")
        corrected = RowSpool(spool_dir / "corrected-proofs.jsonl")
        generations = iter(range(3))

        def write_output(directory, rows):
            write_spooled_tables(directory, rows,
                                 spool_dir / f"tables-{next(generations)}")

        write_quality = write_document
    guard = RuntimeGuard(work)
    # Never export a stale 'valid' status after environment/policy changes.
    for proof in tables["proofs"]:
        if proof["verification_status"] in {"valid", "invalid"}:
            p = problems[proof["problem_id"]]
            env_id = p["environment_id"]
            try:
                env, runtime = guard.get(env_id)
                key = cache_key(p, proof, env, backend=runtime.get("runner", "process"))
                cache = work / "verification-cache" / f"{key}.json"
                matches = (
                    cache.exists()
                    and load_check(cache, key, {"valid", "invalid"})["status"]
                    == proof["verification_status"]
                )
            except (ValueError, CheckCacheCorruption):
                matches = False
            if not matches:
                proof["verification_status"] = "unverified"
                stale.append(proof["id"])
        if spool_dir is not None:
            corrected.append(proof)
    if spool_dir is not None:
        tables["proofs"] = corrected
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        dest = Path(temporary) / "release"
        write_output(dest, tables)
        if spool_dir is None:
            verified = [p for p in tables["proofs"] if p["verification_status"] == "valid"]
            verified_ids = {p["problem_id"] for p in verified}
            verified_problems = [p for p in tables["problems"] if p["id"] in verified_ids]
        else:
            verified = RowSpool(spool_dir / "verified-proofs.jsonl")
            verified_ids = DiskMap(spool_dir / "verified-ids.sqlite")
            resources.callback(verified_ids.close)
            for proof in tables["proofs"]:
                if proof["verification_status"] == "valid":
                    verified.append(proof)
                    verified_ids[proof["problem_id"]] = True
            verified_problems = RowSpool(spool_dir / "verified-problems.jsonl")
            for problem in tables["problems"]:
                if problem["id"] in verified_ids:
                    verified_problems.append(problem)
        write_output(dest / "views" / "proof-discovery", {**tables, "proofs": []})
        write_output(
            dest / "views" / "verified",
            {
                **tables,
                "problems": verified_problems,
                "proofs": verified,
            },
        )
        review = [] if spool_dir is None else RowSpool(spool_dir / "review.jsonl")
        for source in tables["provenance"]:
            sid = source["id"]
            snapshot = work / "snapshots" / sid
            manifest = check_snapshot(snapshot)
            if digest(manifest) != source["manifest_hash"]:
                raise ValueError("provenance manifest hash mismatch")
            target = dest / "snapshots" / sid
            target.mkdir(parents=True)
            shutil.copyfile(snapshot / "manifest.json", target / "manifest.json")
            shutil.copyfile(snapshot / "receipt.json", target / "receipt.json")
            for relative in manifest["license_paths"]:
                relative = safe_relative(relative)
                artifact = target / "original" / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(snapshot / "original" / relative, artifact)
            review.extend(
                {"source_id": sid, "reason": reason}
                for reason in manifest["release_review"]
            )
            shutil.copytree(work / "locks", dest / "locks", dirs_exist_ok=True)
        if spool_dir is None:
            env_ids = {p["environment_id"] for p in tables["problems"]}
        else:
            env_ids = DiskMap(spool_dir / "environments.sqlite")
            resources.callback(env_ids.close)
            for problem in tables["problems"]:
                env_ids[problem["environment_id"]] = True
        for env_id in (sorted(env_ids) if spool_dir is None else env_ids):
            path = work / "environments" / f"{env_id}.json"
            env = read_json(path)
            if (
                __import__("lean_corpus.common", fromlist=["identity"]).identity(
                    "environment", env
                )
                != env_id
            ):
                raise ValueError("environment manifest identity mismatch")
            write_json(dest / "environments" / path.name, env)
        for name in ("resolutions", "environment-projects"):
            if (work / name).exists():
                shutil.copytree(work / name, dest / name)
        if (work / "reports").exists():
            shutil.copytree(work / "reports", dest / "reports")
        # Cache artifacts contain only isolated Lean logs and portable identifiers.
        for name in ("verification-cache", "statement-cache"):
            if (work / name).exists():
                _copy_cache_tree(work / name, dest / name)
        for file in (sources, pipeline, "pyproject.toml", "uv.lock", "lean-toolchain"):
            p = Path(file)
            if p.is_file():
                (dest / "build").mkdir(exist_ok=True)
                shutil.copyfile(p, dest / "build" / p.name)
        by_source = defaultdict(Counter)
        if spool_dir is None:
            source_keys = {r["id"]: r["dataset_name"] for r in tables["provenance"]}
        else:
            source_keys = DiskMap(spool_dir / "source-keys.sqlite")
            resources.callback(source_keys.close)
            for row in tables["provenance"]:
                source_keys[row["id"]] = row["dataset_name"]
        for name in ("problems", "proofs"):
            for row in tables[name]:
                for key in {
                    source_keys[ref["source_id"]] for ref in row["source_refs"]
                }:
                    by_source[key][name] += 1
        counts = {name: len(rows) for name, rows in tables.items()}
        quality = {
            "counts": counts,
            "by_source_overlapping": dict(by_source),
            "verification_status": dict(
                Counter(p["verification_status"] for p in tables["proofs"])
            ),
            "stale_statuses_reset": stale,
            "release_review": review,
        }
        write_quality(dest / "quality.json", quality)
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip()
            dirty = bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain", "--untracked-files=normal"],
                    text=True,
                )
            )
        except subprocess.CalledProcessError:
            revision, dirty = None, True
        write_json(
            dest / "schema.json",
            {"version": VERSION, "tables": {k: str(s) for k, s in SCHEMAS.items()}},
        )
        code_files = sorted(Path(__file__).parent.rglob("*.py")) + sorted(
            Path(__file__).parent.rglob("*.lean")
        )
        write_json(
            dest / "build.json",
            {
                "code_revision": revision,
                "dirty_worktree": dirty,
                "implementation_version": __version__,
                "code_digest": digest(
                    {
                        p.relative_to(Path(__file__).parent).as_posix(): file_hash(p)
                        for p in code_files
                    }
                ),
                "schema_version": VERSION,
                "logical_order": "lexicographic content ID",
            },
        )
        (dest / "DATA_LICENSE.md").write_text(DATA_LICENSE + "\n")
        (dest / "README.md").write_text(
            "# Proof-discovery corpus\n\n"
            + DATA_LICENSE
            + "\n\nThird-party attribution and original license declarations are under snapshots/<source-id>/original/. Acquisition manifests inventory the complete originals by path and SHA-256; payloads are retained in the local acquisition workspace, not duplicated in this release. Environment manifests are under environments/.\n\nPutnamBench is eligible training content, not held out. Source exposure is reported in quality.json. Deduplication does not establish absence of benchmark contamination. No train/dev/test split is assigned. Verification checks formal consistency, not semantic correspondence to informal mathematics.\n\nThe proof-discovery view contains problems; the verified view selects only locally valid attempts. Unresolved environments and unsupported extraction forms are reported. Merged source counts overlap. Statement elaboration is not a proof of provability. Publication is a separate user action.\n"
        )
        guard.finish()
        require_current(work)
        if input_hashes is not None and input_hashes != {
            name: file_hash(work / "canonical" / f"{name}.parquet") for name in SCHEMAS
        }:
            raise ValueError("canonical data changed during export")
        dest.rename(output)
    if spool_dir is not None:
        return {"counts": quality["counts"],
                "verification_status": quality["verification_status"],
                "quality_report": str(output / "quality.json")}
    return quality
