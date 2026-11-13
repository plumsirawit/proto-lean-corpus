"""Immutable local snapshots. Network routes are explicit; processing never fetches."""

import fnmatch
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import yaml
from .common import digest, file_hash, identity, read_json, write_json

EXCLUDED = {".git", ".lake", ".venv", "__pycache__"}


def registry(path):
    data = yaml.safe_load(Path(path).read_text())
    return data["sources"]


def inventory(root):
    result = []
    for p in sorted(Path(root).rglob("*")):
        if any(x in EXCLUDED for x in p.relative_to(root).parts):
            continue
        if p.is_symlink():
            raise ValueError(
                f"symlinks are not supported in snapshots: {p.relative_to(root)}"
            )
        if p.is_file():
            result.append(
                {
                    "path": p.relative_to(root).as_posix(),
                    "size": p.stat().st_size,
                    "sha256": file_hash(p),
                }
            )
    return result


def check_snapshot(path):
    path = Path(path)
    manifest = read_json(path / "manifest.json")
    if inventory(path / "original") != manifest["files"]:
        raise ValueError(f"snapshot integrity failure: {path.name}")
    expected = identity("source", manifest)
    if expected != path.name:
        raise ValueError("manifest identity mismatch")
    return manifest


def project_environment(root, source, hints=None):
    root = Path(root)
    config = {}
    for name in (
        "lean-toolchain",
        "lakefile.lean",
        "lakefile.toml",
        "lake-manifest.json",
    ):
        p = root / name
        if p.exists():
            config[name] = p.read_text()
    toolchain = config.get("lean-toolchain", "").strip() or None
    packages = []
    if "lake-manifest.json" in config:
        lock = __import__("json").loads(config["lake-manifest.json"])
        packages = [
            {k: p.get(k) for k in ("name", "url", "rev", "type", "dir")}
            for p in lock.get("packages", [])
        ]
    # No inferred latest dependency versions. A declaration of a dependency without a
    # resolved lock is not a complete environment.
    has_lake = "lakefile.lean" in config or "lakefile.toml" in config
    pinned = all(
        p.get("type") == "git" and isinstance(p.get("rev"), str) and len(p["rev"]) == 40
        for p in packages
    )
    resolved = bool(
        toolchain and (not has_lake or "lake-manifest.json" in config) and pinned
    )
    return {
        "schema_version": "1",
        "toolchain": toolchain,
        "packages": packages,
        "project_files": [
            f
            for f in inventory(root)
            if f["path"].endswith((".lean", ".toml"))
            or f["path"] in {"lean-toolchain", "lake-manifest.json"}
        ],
        "build_config": config,
        "resolved": resolved,
        "hints": (hints or {}) if not resolved else {},
        "unresolved_scope": {
            "source_key": source,
            "input_digest": digest(inventory(root)),
        }
        if not resolved
        else None,
    }


def acquire(work, sources, key, local=None, revision=None, patterns=None):
    config = registry(sources)[key]
    if config["status"] != "active":
        raise ValueError(f"{key} is deferred: {config.get('reason')}")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as staging:
        staging = Path(staging)
        original = staging / "original"
        route = "local" if local else config["route"]
        resolved = revision
        if local:
            root = Path(local).resolve()
            if work.resolve().is_relative_to(root):
                raise ValueError("work directory must not be inside imported snapshot")
            shutil.copytree(
                root, original, ignore=shutil.ignore_patterns(*EXCLUDED), symlinks=True
            )
            files = inventory(original)
            # A caller's label is not evidence of an upstream revision for a local copy.
            resolved = "content-sha256:" + digest(files)
        elif route == "hf":
            from huggingface_hub import HfApi, snapshot_download

            info = HfApi().dataset_info(config["repo"], revision=revision or "main")
            resolved = info.sha
            selected = patterns or [
                "*.parquet",
                "*.json",
                "*.jsonl",
                "**/*.parquet",
                "**/*.jsonl",
            ]
            # Select notices from pinned metadata case-insensitively; bounded data
            # globs must never accidentally omit a supplied license or attribution.
            selected = selected + [
                f.rfilename
                for f in info.siblings
                if Path(f.rfilename).suffix.lower() == ".md"
                or any(
                    term in Path(f.rfilename).name.upper()
                    for term in ("README", "LICENSE", "NOTICE", "COPYING", "AUTHORS")
                )
            ]
            snapshot_download(
                config["repo"],
                repo_type="dataset",
                revision=resolved,
                local_dir=original,
                allow_patterns=selected,
            )
            shutil.rmtree(original / ".cache", ignore_errors=True)
        elif route == "git":
            repo = staging / "checkout"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    "--filter=blob:none",
                    config["url"],
                    str(repo),
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "--detach", revision or "HEAD"],
                check=True,
            )
            resolved = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            shutil.copytree(
                repo, original, ignore=shutil.ignore_patterns(*EXCLUDED), symlinks=True
            )
        else:
            raise ValueError(
                f"unsupported acquisition route: {route}; import bucket contents using --local"
            )
        files = inventory(original)
        if not files:
            raise ValueError("empty acquisition")
        licenses = [
            f["path"]
            for f in files
            if any(
                s in Path(f["path"]).name.upper()
                for s in ("LICENSE", "NOTICE", "COPYING", "README", "AUTHORS")
            )
        ]
        env = project_environment(
            original / config.get("project_subdir", ""),
            key,
            config.get("environment_hints"),
        )
        env_id = identity("environment", env)
        write_json(work / "environments" / f"{env_id}.json", env)
        manifest = {
            "schema_version": "1",
            "source_key": key,
            "url": config["url"],
            "route": route,
            "revision": resolved,
            "revision_label": revision if local else None,
            "selection": patterns,
            "files": files,
            "license_paths": licenses,
            "declared_license": config["license"],
            "authorship": config.get("authorship"),
            "environment_id": env_id,
            "project_subdir": config.get("project_subdir", ""),
            "modifications": [],
            "release_review": (
                []
                if any(
                    any(term in Path(p).name.upper() for term in ("LICENSE", "COPYING"))
                    for p in licenses
                )
                else [
                    "No standalone upstream license text found; review declared terms and attribution"
                ]
            ),
        }
        source_id = identity("source", manifest)
        destination = work / "snapshots" / source_id
        if destination.exists():
            check_snapshot(destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_json(staging / "manifest.json", manifest)
            write_json(
                staging / "receipt.json",
                {"acquired_at": datetime.now(timezone.utc).isoformat()},
            )
            ready = staging / "ready"
            ready.mkdir()
            original.rename(ready / "original")
            (staging / "manifest.json").rename(ready / "manifest.json")
            (staging / "receipt.json").rename(ready / "receipt.json")
            ready.rename(destination)
        # Lock points to an immutable source snapshot; new acquisition adds rather than overwrites.
        write_json(
            work / "locks" / f"{source_id}.json",
            {"source_key": key, "revision": resolved, "snapshot_id": source_id},
        )
    return source_id


def selected_snapshots(work, selected=None):
    snapshots = sorted((Path(work) / "snapshots").glob("source-v1-*"))
    result = []
    for path in snapshots:
        manifest = check_snapshot(path)
        if not selected or manifest["source_key"] in selected:
            result.append((path, manifest))
    if not result:
        raise ValueError("no acquired snapshots match source selection")
    return result


def included(path, patterns):
    if not patterns:
        return True
    for pattern in patterns:
        variants = {pattern, pattern.replace("/**/", "/")}
        if pattern.startswith("**/"):
            variants.add(pattern[3:])
        if any(fnmatch.fnmatch(path, variant) for variant in variants):
            return True
    return False
