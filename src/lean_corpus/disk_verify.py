"""Opt-in verification with job-local indexes and complete streamed reports.

The existing checker, caches, budgets and acceptance policy remain authoritative.
A failure retains spools and a publication marker; a new spool directory is needed
for a retry. Cache files stay in the workspace for the existing recovery driver.
"""

from collections import Counter
from pathlib import Path

from .check_cache import load_check, store_check
from .common import read_json, write_json
from .deduplicate import require_current
from .disk_merge import parquet_rows
from .disk_output import DiskMap, RowSpool, write_document, write_spooled_tables
from .disk_schema import validate_parquet
from .lean import POLICY
from .runtime import RuntimeGuard
from .schema import SCHEMAS
from .verify import cache_key, check_problem, check_proof


def verify_spooled(work, spool_dir, limit=None, timeout=60):
    work, spool_dir = Path(work), Path(spool_dir)
    require_current(work)
    marker = work / "verification-incomplete.json"
    write_json(marker, {"state": "running"})
    maps = []
    try:
        spool_dir.mkdir()
        original_state = read_json(work / "canonical/state.json")
        validate_parquet(work / "canonical", spool_dir / "input.sqlite")
        inputs = {name: RowSpool(spool_dir / f"input-{name}.jsonl") for name in SCHEMAS}
        for name, row in parquet_rows(work / "canonical"):
            if name == "provenance":
                row = {**row, "acquired_at": row["acquired_at"].isoformat()}
            inputs[name].append(row)
        problems = DiskMap(spool_dir / "problems.sqlite")
        maps.append(problems)
        baselines = DiskMap(spool_dir / "baselines.sqlite")
        maps.append(baselines)
        for problem in inputs["problems"]:
            problems[problem["id"]] = problem
        reports = RowSpool(spool_dir / "attempts.jsonl")
        created_caches = RowSpool(spool_dir / "created-caches.jsonl")
        output = RowSpool(spool_dir / "output-proofs.jsonl")
        counts = Counter()
        guard = RuntimeGuard(work)

        def baseline_for(problem, environment, runtime):
            key = cache_key(problem, None, environment,
                            backend=runtime.get("runner", "process"))
            cache = work / "statement-cache" / f"{key}.json"
            if cache.exists():
                return load_check(cache, key, {"elaborated", "invalid"})
            result = check_problem(problem, environment, runtime, timeout)
            if result["status"] in {"elaborated", "invalid"}:
                store_check(cache, key, result)
                created_caches.append(str(cache))
            return result

        checked = 0
        for proof in inputs["proofs"]:
            if limit is None or checked < limit:
                problem = problems[proof["problem_id"]]
                try:
                    environment, runtime = guard.get(problem["environment_id"])
                except ValueError as error:
                    proof["verification_status"] = "inconclusive"
                    reports.append({"proof_id": proof["id"], "status": "inconclusive",
                                    "reason": "runtime_mismatch", "detail": str(error)})
                else:
                    key = cache_key(problem, proof, environment,
                                    backend=runtime.get("runner", "process"))
                    cache = work / "verification-cache" / f"{key}.json"
                    if cache.exists():
                        result = load_check(cache, key, {"valid", "invalid"})
                    else:
                        if problem["id"] not in baselines:
                            baselines[problem["id"]] = baseline_for(
                                problem, environment, runtime)
                        result = check_proof(problem, proof, environment, runtime,
                                             baselines[problem["id"]], timeout)
                        if result["status"] in {"valid", "invalid"}:
                            store_check(cache, key, result)
                            created_caches.append(str(cache))
                    proof["verification_status"] = result["status"]
                    reports.append({"proof_id": proof["id"], "cache_key": key, **result})
                checked += 1
            output.append(proof)
            counts[proof["verification_status"]] += 1

        problem_budget = len(inputs["problems"]) if limit is None else limit
        # Preserve the existing list[:limit] semantics, including negative limits.
        if problem_budget < 0:
            problem_budget = max(0, len(inputs["problems"]) + problem_budget)
        for index, problem in enumerate(inputs["problems"]):
            if index >= problem_budget:
                break
            if problem["id"] not in baselines:
                try:
                    environment, runtime = guard.get(problem["environment_id"])
                    baselines[problem["id"]] = baseline_for(problem, environment, runtime)
                except ValueError as error:
                    baselines[problem["id"]] = {
                        "status": "inconclusive", "reason": "runtime_mismatch",
                        "detail": str(error),
                    }
        try:
            guard.finish()
            require_current(work)
            if read_json(work / "canonical/state.json") != original_state:
                raise ValueError("canonical data changed during verification")
        except BaseException:
            for path in created_caches:
                Path(path).unlink(missing_ok=True)
            raise
        write_spooled_tables(work / "canonical", {**inputs, "proofs": output},
                             spool_dir / "tables")
        write_document(work / "reports/verification.json", {
            "policy": POLICY, "limit": limit, "attempts": reports, "problems": baselines,
        })
    except BaseException as error:
        write_json(marker, {"state": "failed", "exception": type(error).__name__,
                            "reason": str(error)})
        raise
    finally:
        for mapping in maps:
            mapping.close()
    marker.unlink()
    return {status: counts[status]
            for status in ("unverified", "valid", "invalid", "inconclusive")}
