from pathlib import Path
from datetime import datetime, timezone
from .acquire import included, registry, selected_snapshots
from .adapters import ADAPTERS, record_adapter
from .adapters.records import PUTNAM_UPSTREAM_REVISION, library, rows
from .common import digest, identity, read_json, text, write_json
from .environment import environment_for
from .extraction_cache import ExtractionCache, INPUT_CHECK_LIMITS, implementation_digest
from .lean import LeanFailure
from .schema import write_tables


def runtime_for(work, env_id):
    path = Path(work) / "runtimes" / f"{env_id}.json"
    return read_json(path) if path.exists() else {}


def normalize(work, sources, selected=None, limit=None, selection=None):
    work = Path(work)
    marker = work / "normalization-incomplete.json"
    state = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "selected_sources": selected,
        "limit": limit,
        "selection": str(selection) if selection is not None else None,
    }
    write_json(marker, state)
    progress = {}
    try:
        result = _normalize(work, sources, selected, limit, progress, selection)
    except BaseException as error:
        write_json(
            marker,
            {
                **state,
                **progress,
                "state": "failed",
                "exception": type(error).__name__,
                "reason": str(error),
            },
        )
        raise
    marker.unlink()
    return result


def _normalize(work, sources, selected, limit, progress, selection):
    # Deferred import: the verifier imports runtime_for from this module.
    from .verify import validate_runtime

    cache = ExtractionCache(work / "extraction-cache")
    chosen = read_json(selection) if selection is not None else None
    requested = set()
    if chosen is not None:
        if limit is not None:
            raise ValueError("selection and limit are mutually exclusive")
        if (
            not isinstance(chosen, dict)
            or chosen.get("schema_version") != "1"
            or not isinstance(chosen.get("snapshots"), dict)
        ):
            raise ValueError("invalid selection manifest")
        for sid, files in chosen["snapshots"].items():
            if not isinstance(files, dict):
                raise ValueError("selection files must be a mapping")
            for name, indices in files.items():
                if (
                    not isinstance(indices, list)
                    or any(
                        not (
                            (type(i) is int and i >= 0)
                            or (isinstance(i, str) and bool(i.strip()))
                        )
                        for i in indices
                    )
                    or len(set(indices)) != len(indices)
                ):
                    raise ValueError(
                        "selection rows must be unique nonnegative integers or nonempty keys"
                    )
                requested.update((sid, name, i) for i in indices)
        if not requested:
            raise ValueError("selection must include at least one row")
    remaining = set(requested)
    config = registry(sources)
    tables = {"problems": [], "proofs": [], "provenance": []}
    reports, mapping, transforms = [], [], []
    count = 0
    for snapshot, manifest in selected_snapshots(work, selected):
        key = manifest["source_key"]
        if key not in ADAPTERS:
            raise ValueError(f"no active adapter: {key}")
        revision = manifest.get("revision_label") or manifest["revision"]
        adapter = record_adapter(key, revision)
        pinned_putnam = key == "putnam_bench" and revision == PUTNAM_UPSTREAM_REVISION
        sid = snapshot.name
        env_id = environment_for(work, sid, manifest)
        env = read_json(work / "environments" / f"{env_id}.json")
        runtime = runtime_for(work, env_id)
        validate_runtime(env, runtime)
        receipt = read_json(snapshot / "receipt.json")
        tables["provenance"].append(
            {
                "id": sid,
                "dataset_name": key,
                "revision": manifest["revision"],
                "acquired_at": receipt["acquired_at"],
                "manifest_hash": digest(manifest),
                "license_refs": [
                    f"snapshots/{sid}/original/{p}" for p in manifest["license_paths"]
                ],
            }
        )
        root = snapshot / "original"
        for entry in manifest["files"]:
            relative = entry["path"]
            path = root / relative
            if chosen is not None and relative not in chosen["snapshots"].get(sid, {}):
                continue
            if limit is not None and count >= limit:
                break
            is_library = key in {"mathlib4", "tau_ceti"} or (
                key == "putnam_bench" and path.suffix == ".lean"
            )
            if is_library:
                if (
                    path.suffix != ".lean"
                    or path.name == "lakefile.lean"
                    or (pinned_putnam and not relative.startswith("lean4/src/"))
                    or not included(relative, config[key].get("include"))
                ):
                    continue
            elif path.suffix not in {
                ".json",
                ".jsonl",
                ".csv",
                ".parquet",
            } or path.name in {"lake-manifest.json", "manifest.json"}:
                continue
            try:
                stream = (
                    (
                        (f"declaration={x.get('target')}", x)
                        for x in library(path, env, runtime, putnam=pinned_putnam)
                    )
                    if is_library
                    else rows(path)
                )
                for index, row in stream:
                    if limit is not None and count >= limit:
                        break
                    if chosen is not None:
                        occurrence = (sid, relative, index)
                        if occurrence not in requested:
                            continue
                        remaining.discard(occurrence)
                    count += 1
                    location = (
                        f"{relative}#{index}"
                        if is_library
                        else f"{relative}#row={index}"
                    )
                    progress.update(
                        source_id=sid, location=location, input_occurrences=count
                    )
                    try:
                        if not isinstance(row, dict) or "_malformed" in row:
                            raise ValueError("malformed input record")
                        if "error" in row:
                            raise ValueError(row["error"])
                        result = (
                            row
                            if is_library
                            else cache.extract(
                                sid,
                                entry,
                                index,
                                key,
                                env,
                                lambda: adapter(row, env, runtime),
                            )
                        )
                        pid = identity(
                            "problem",
                            {
                                "source": text(result["task"]),
                                "target": result["target"],
                                "environment": env_id,
                            },
                        )
                        ref = {
                            "source_id": sid,
                            "upstream_id": result["upstream_id"],
                            "location": location
                            + "&field="
                            + result.get("statement_field", "formal_statement"),
                        }
                        tables["problems"].append(
                            {
                                "id": pid,
                                "formal_statement": result["task"],
                                "informal_statement": result["informal"],
                                "target_name": result["target"],
                                "environment_id": env_id,
                                "source_refs": [ref],
                            }
                        )
                        mapping.append(
                            {
                                "source_id": sid,
                                "location": ref["location"],
                                "problem_id": pid,
                            }
                        )
                        for attempt in result["attempts"]:
                            term = text(attempt["proof"])
                            proof_id = identity("proof", {"problem": pid, "term": term})
                            aref = {
                                **ref,
                                "location": location + "&field=" + attempt["field"],
                            }
                            tables["proofs"].append(
                                {
                                    "id": proof_id,
                                    "problem_id": pid,
                                    "formal_proof": term,
                                    "informal_proof": attempt["informal"],
                                    "verification_status": "unverified",
                                    "source_refs": [aref],
                                }
                            )
                            mapping.append(
                                {
                                    "source_id": sid,
                                    "location": aref["location"],
                                    "problem_id": pid,
                                    "proof_id": proof_id,
                                }
                            )
                        reports.extend(
                            {
                                "source_id": sid,
                                "location": location + "&field=" + r["field"],
                                "kind": r["kind"],
                                "reason": r["reason"],
                            }
                            for r in result["reports"]
                        )
                        if result["transformations"]:
                            transforms.append(
                                {
                                    "source_id": sid,
                                    "location": location,
                                    "changes": result["transformations"],
                                }
                            )
                    except LeanFailure as e:
                        if e.reason in INPUT_CHECK_LIMITS:
                            kind = "inconclusive"
                        elif e.reason == "unsupported_syntax":
                            kind = "unsupported"
                        else:
                            raise
                        reports.append(
                            {
                                "source_id": sid,
                                "location": location,
                                "kind": kind,
                                "reason": str(e),
                            }
                        )
                    except ValueError as e:
                        reports.append(
                            {
                                "source_id": sid,
                                "location": location,
                                "kind": "malformed"
                                if "malformed" in str(e)
                                else "unsupported",
                                "reason": str(e),
                            }
                        )
            except LeanFailure as e:
                if e.reason != "unsupported_syntax":
                    raise
                reports.append(
                    {
                        "source_id": sid,
                        "location": relative,
                        "kind": "unsupported",
                        "reason": str(e),
                    }
                )
            except ValueError as e:
                reports.append(
                    {
                        "source_id": sid,
                        "location": relative,
                        "kind": "unsupported",
                        "reason": str(e),
                    }
                )
    if remaining:
        raise ValueError(
            f"selection contains {len(remaining)} missing or unprocessed rows"
        )
    if implementation_digest() != cache.implementation:
        raise RuntimeError(
            "implementation changed during normalization; rerun with frozen code"
        )
    # Stage replacement is deterministic; reruns never append duplicate artifacts.
    write_tables(work / "normalized", tables, unique=False)
    write_json(
        work / "reports" / "extraction.json",
        {
            "input_occurrences": count,
            "problem_occurrences": len(tables["problems"]),
            "proof_occurrences": len(tables["proofs"]),
            "limit": limit,
            "sources": selected,
            "reports": reports,
            "selection": chosen,
            "cache_hits": cache.hits,
            "cache_misses": cache.misses,
        },
    )
    write_json(work / "reports" / "occurrences.json", mapping)
    write_json(work / "reports" / "transformations.json", transforms)
    # Changing normalized inputs invalidates all downstream stage products.
    write_json(
        work / "normalized" / "state.json",
        {"fingerprint": digest(tables)},
    )
    return {
        "problems": len(tables["problems"]),
        "proofs": len(tables["proofs"]),
        "reports": len(reports),
    }
