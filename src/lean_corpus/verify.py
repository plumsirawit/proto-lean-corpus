from pathlib import Path
from . import __version__
from .common import digest, file_hash, read_json, write_json
from .deduplicate import require_current
from .lean import (
    POLICY,
    LeanFailure,
    implementation_hash,
    inspect,
    replace_span,
    slice_span,
    target_info,
)
from .runtime import RuntimeGuard, validate_runtime as validate_runtime
from .schema import read_tables, write_tables


def cache_key(problem, proof, environment, policy=POLICY, backend="process"):
    # Include actual records as well as IDs to fail safely on corrupted/manual edits.
    return digest(
        {
            "task": problem["formal_statement"],
            "problem_id": problem["id"],
            "target": problem["target_name"],
            "proof": proof["formal_proof"] if proof else None,
            "environment": environment,
            "policy": policy,
            "backend": backend,
            "worker": file_hash(Path(__file__).with_name("worker.py")),
            "runtime_guard": file_hash(Path(__file__).with_name("runtime.py")),
            "sources": problem.get("source_refs", []),
            "attempt_sources": proof.get("source_refs", []) if proof else [],
            "implementation": __version__,
            "helper": implementation_hash(),
            "runner": file_hash(Path(__file__)),
            "sandbox": file_hash(Path(__file__).with_name("isolation.py")),
            "interface": file_hash(Path(__file__).with_name("lean.py")),
        }
    )


def check_problem(problem, environment, runtime, timeout=60):
    if not environment["resolved"]:
        return {"status": "inconclusive", "reason": "unresolved_environment"}
    try:
        target = target_info(
            problem["formal_statement"], environment, runtime, problem["target_name"]
        )
        if target["name"] != problem["target_name"]:
            return {"status": "invalid", "reason": "target_mismatch"}
        result = inspect(
            problem["formal_statement"],
            environment,
            runtime,
            "verify",
            problem["target_name"],
            timeout,
        )
        if result.get("supporting_admissions"):
            return {
                "status": "invalid",
                "reason": "supporting_context_admission",
                "inspection": result,
            }
        if result["errors"] or not result.get("is_theorem"):
            return {
                "status": "invalid",
                "reason": "statement_elaboration_failure",
                "inspection": result,
            }
        return {
            "status": "elaborated",
            "reason": "statement_only_not_provability",
            "target": target,
            "type": result["type"],
            "inspection": result,
        }
    except ValueError as e:
        return {"status": "invalid", "reason": "structural_invariant", "detail": str(e)}
    except LeanFailure as e:
        return {"status": "inconclusive", "reason": e.reason, "detail": e.detail}


def check_proof(problem, proof, environment, runtime, baseline, timeout=60):
    if baseline["status"] != "elaborated":
        return {"status": baseline["status"], "reason": "problem_" + baseline["reason"]}
    try:
        source = replace_span(
            problem["formal_statement"],
            baseline["target"]["body"],
            proof["formal_proof"],
        )
        result = inspect(
            source, environment, runtime, "verify", problem["target_name"], timeout
        )
        if result["errors"]:
            return {
                "status": "invalid",
                "reason": "proof_elaboration_failure",
                "inspection": result,
            }
        # Injection of commands, additional declarations or admissions is rejected.
        targets = [t for t in result["targets"] if t["name"] == problem["target_name"]]
        if (
            len(targets) != 1
            or slice_span(source, targets[0]["body"]) != proof["formal_proof"]
            or not result.get("is_theorem")
            or result.get("type") != baseline["type"]
        ):
            return {
                "status": "invalid",
                "reason": "target_changed",
                "inspection": result,
            }
        unauthorized = sorted(
            set(result.get("axioms", [])) - set(POLICY["allowed_axioms"])
        )
        if unauthorized or result["admissions"] or result.get("supporting_admissions"):
            return {
                "status": "invalid",
                "reason": "admission_or_unauthorized_axiom",
                "unauthorized_axioms": unauthorized,
                "inspection": result,
            }
        if "axioms" not in result:
            return {"status": "inconclusive", "reason": "axiom_inspection_missing"}
        return {
            "status": "valid",
            "reason": "kernel_and_axiom_checks_passed",
            "inspection": result,
        }
    except LeanFailure as e:
        # Missing imports/tooling, parser protocol errors and resource failures
        # are never interpreted as mathematical invalidity.
        return {"status": "inconclusive", "reason": e.reason, "detail": e.detail}


def verify(work, limit=None, timeout=60):
    work = Path(work)
    require_current(work)
    tables = read_tables(work / "canonical")
    problems = {p["id"]: p for p in tables["problems"]}
    baselines, reports = {}, []
    guard = RuntimeGuard(work)
    created_caches = []
    checked = 0
    for proof in tables["proofs"]:
        if limit is not None and checked >= limit:
            break
        problem = problems[proof["problem_id"]]
        env_id = problem["environment_id"]
        # Validate recorded configuration against the runtime before trusting it.
        try:
            environment, runtime = guard.get(env_id)
        except ValueError as e:
            proof["verification_status"] = "inconclusive"
            reports.append(
                {
                    "proof_id": proof["id"],
                    "status": "inconclusive",
                    "reason": "runtime_mismatch",
                    "detail": str(e),
                }
            )
            checked += 1
            continue
        key = cache_key(
            problem, proof, environment, backend=runtime.get("runner", "process")
        )
        cache = work / "verification-cache" / f"{key}.json"
        if cache.exists():
            result = read_json(cache)
        else:
            if problem["id"] not in baselines:
                baseline = check_problem(problem, environment, runtime, timeout)
                baselines[problem["id"]] = baseline
            result = check_proof(
                problem, proof, environment, runtime, baselines[problem["id"]], timeout
            )
            # Infrastructure failures are retried on future runs.
            if result["status"] in {"valid", "invalid"}:
                write_json(cache, result)
                created_caches.append(cache)
        proof["verification_status"] = result["status"]
        reports.append({"proof_id": proof["id"], "cache_key": key, **result})
        checked += 1
    # Also elaborate problem-only records within the bounded problem budget.
    for problem in tables["problems"][:limit]:
        if problem["id"] not in baselines:
            env_id = problem["environment_id"]
            try:
                environment, runtime = guard.get(env_id)
                baselines[problem["id"]] = check_problem(
                    problem, environment, runtime, timeout
                )
            except ValueError as e:
                baselines[problem["id"]] = {
                    "status": "inconclusive",
                    "reason": "runtime_mismatch",
                    "detail": str(e),
                }
    try:
        guard.finish()
        require_current(work)
    except BaseException:
        for path in created_caches:
            path.unlink(missing_ok=True)
        raise
    write_tables(work / "canonical", tables)
    write_json(
        work / "reports" / "verification.json",
        {"policy": POLICY, "limit": limit, "attempts": reports, "problems": baselines},
    )
    return {
        status: sum(p["verification_status"] == status for p in tables["proofs"])
        for status in ("unverified", "valid", "invalid", "inconclusive")
    }
