from collections import Counter, defaultdict
from pathlib import Path
import shutil
import subprocess
import tempfile
from . import __version__
from .acquire import check_snapshot
from .common import VERSION, digest, file_hash, read_json, safe_relative, write_json
from .deduplicate import require_current
from .schema import SCHEMAS, read_tables, write_tables
from .verify import cache_key, validate_runtime
from .normalize import runtime_for

DATA_LICENSE = "Our original contributions and compilation/database rights, to the extent applicable, are licensed under CC BY-SA 4.0. Third-party material retains its applicable upstream licenses, identified in the source manifest and accompanying notices. This license does not replace or restrict permissions independently granted by upstream licensors."


def export(
    work, output, sources="configs/sources.yaml", pipeline="configs/pipeline.yaml"
):
    work, output = Path(work), Path(output)
    require_current(work)
    if output.exists():
        raise ValueError("export destination already exists; use a new directory")
    tables = read_tables(work / "canonical")
    stale = []
    problems = {r["id"]: r for r in tables["problems"]}
    # Never export a stale 'valid' status after environment/policy changes.
    for proof in tables["proofs"]:
        if proof["verification_status"] in {"valid", "invalid"}:
            p = problems[proof["problem_id"]]
            env = read_json(work / "environments" / f"{p['environment_id']}.json")
            key = cache_key(
                p,
                proof,
                env,
                backend=runtime_for(work, p["environment_id"]).get("runner", "process"),
            )
            cache = work / "verification-cache" / f"{key}.json"
            try:
                validate_runtime(env, runtime_for(work, p["environment_id"]))
                matches = (
                    cache.exists()
                    and read_json(cache)["status"] == proof["verification_status"]
                )
            except ValueError:
                matches = False
            if not matches:
                proof["verification_status"] = "unverified"
                stale.append(proof["id"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        dest = Path(temporary) / "release"
        write_tables(dest, tables)
        verified = [p for p in tables["proofs"] if p["verification_status"] == "valid"]
        verified_ids = {p["problem_id"] for p in verified}
        write_tables(dest / "views" / "proof-discovery", {**tables, "proofs": []})
        write_tables(
            dest / "views" / "verified",
            {
                **tables,
                "problems": [p for p in tables["problems"] if p["id"] in verified_ids],
                "proofs": verified,
            },
        )
        review = []
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
        env_ids = {p["environment_id"] for p in tables["problems"]}
        for env_id in sorted(env_ids):
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
        if (work / "verification-cache").exists():
            shutil.copytree(work / "verification-cache", dest / "verification-cache")
        for file in (sources, pipeline, "pyproject.toml", "uv.lock", "lean-toolchain"):
            p = Path(file)
            if p.is_file():
                (dest / "build").mkdir(exist_ok=True)
                shutil.copyfile(p, dest / "build" / p.name)
        by_source = defaultdict(Counter)
        source_keys = {r["id"]: r["dataset_name"] for r in tables["provenance"]}
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
        write_json(dest / "quality.json", quality)
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
        dest.rename(output)
    return quality
