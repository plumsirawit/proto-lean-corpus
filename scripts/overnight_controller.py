"""Sequential, quota-gated Slurm submission from reviewed immutable step plans.

The agent prepares plans; this controller handles only deterministic transitions.
Every processing step must be followed by a successful independent audit before
another size can be submitted. Missing plans and failed jobs wait for agent repair.
No network downloads, checking, unpacking, deletions, or publication happen here.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from euler_quota import check

SOURCES = ("goedel_prover_v2_sft", "nemotron_math_proofs_v1", "mathlib4", "tau_ceti")
SIZES = (10, 100, 1000, "full")


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def command(args):
    return subprocess.check_output(args, text=True, timeout=45).strip()


def memory_mib(text):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)", text)
    if not match:
        raise ValueError("Unrecognized Slurm memory: " + text)
    return (
        float(match[1])
        * {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024**2}[match[2]]
    )


def allocation():
    queue = command(
        ["squeue", "--local", "-u", os.environ["USER"], "-h", "-o", "%i|%j"]
    )
    cpus, memory = 0, 0
    jobs = []
    for line in queue.splitlines():
        job, name = line.split("|", 1)
        detail = command(["scontrol", "show", "job", "-o", job])
        match = re.search(r"\bReqTRES=(\S+)", detail)
        if not match:
            raise ValueError("Missing requested TRES for job " + job)
        tres = dict(pair.split("=", 1) for pair in match[1].split(","))
        cpus += int(tres["cpu"])
        memory += memory_mib(tres["mem"])
        jobs.append({"job_id": job, "name": name, "requested_tres": tres})
    account = command(
        [
            "sacctmgr",
            "-n",
            "-P",
            "show",
            "assoc",
            "where",
            "user=" + os.environ["USER"],
            "format=Account,Partition,QOS,MaxJobs,MaxSubmit,GrpTRES",
        ]
    )
    if not account:
        raise ValueError("No live account limits returned")
    return {"cpus": cpus, "memory_mib": memory, "jobs": jobs, "account": account}


def validate_plan(plan):
    """Require fixed process/audit pairs in the authorized source and size order."""
    expected = [
        (s, n, kind) for s in SOURCES for n in SIZES for kind in ("process", "audit")
    ]
    steps = plan["steps"]
    if len(steps) > len(expected):
        raise ValueError("Too many planned steps")
    for step, identity in zip(steps, expected):
        if (step["source"], step["size"], step["kind"]) != identity:
            raise ValueError("Plan does not follow source/size/audit gates")
        if not 1 <= step["cpus"] <= 47 or step["mem_per_cpu_mib"] <= 0:
            raise ValueError("Invalid resources")
        if step["cpus"] * step["mem_per_cpu_mib"] > 114000:
            raise ValueError("Plan exceeds total memory cap")
        if not re.fullmatch(r"[0-9a-f]{40}", step["code_revision"]):
            raise ValueError("Immutable code revision required")
        if step["reserve_bytes"] <= 0 or step["reserve_files"] <= 0:
            raise ValueError("Explicit quota reserves required")
        if step["kind"] == "audit" and not step.get("independent_audit"):
            raise ValueError("Independent audit required")
    return steps


def tick(control):
    with (control / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = control / "controller-state.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"index": 0, "history": []}
        )
        state["updated_at"] = now()

        def save(status, detail=None):
            state.update(status=status, detail=detail)
            atomic(path, state)
            print(
                json.dumps({"status": status, "detail": detail, "updated_at": now()}),
                flush=True,
            )

        plan_path = control / "controller-plan.json"
        if not plan_path.exists():
            save("waiting_for_prepared_plan")
            return
        steps = validate_plan(json.loads(plan_path.read_text()))
        index = state["index"]
        if len(state["history"]) != index or index > len(steps):
            raise ValueError("Completed history does not match planned index")
        for completed, planned in zip(state["history"], steps[:index]):
            if completed["step"] != planned:
                raise ValueError("Completed step changed in plan")
        if index >= len(steps):
            save(
                "prepared_steps_audited"
                if index == 32
                else "waiting_for_more_prepared_steps"
            )
            return
        step = steps[index]
        if step["kind"] == "process" and index:
            audited = state["history"][-1]
            inspection_path = control / ("inspection-" + audited["job_id"] + ".json")
            inspection = (
                json.loads(inspection_path.read_text())
                if inspection_path.is_file()
                else {}
            )
            if not (
                inspection.get("reviewed") is True
                and inspection.get("audit_job_id") == audited["job_id"]
                and inspection.get("sha256") == audited["receipt"]["sha256"]
            ):
                save("waiting_for_agent_inspection", audited["job_id"])
                return
        digest = hashlib.sha256(json.dumps(step, sort_keys=True).encode()).hexdigest()
        active = state.get("active")
        if active:
            if active["step_digest"] != digest:
                raise ValueError(
                    "Active step changed; preserve it and repair explicitly"
                )
            if not active.get("job_id"):
                # An uncertain submit reply cannot cause an automatic duplicate.
                save("submission_needs_reconciliation", active["job_name"])
                return
            job = active["job_id"]
            live = command(["squeue", "--local", "-h", "-j", job, "-o", "%T"])
            if live:
                save("job_active", {"job_id": job, "state": live})
                return
            accounting = command(
                [
                    "sacct",
                    "-n",
                    "-P",
                    "-j",
                    job,
                    "--format=JobIDRaw,State,ExitCode,ReqTRES,MaxRSS",
                ]
            )
            active["accounting"] = accounting
            rows = [
                line.split("|")
                for line in accounting.splitlines()
                if line.split("|", 1)[0] == job
            ]
            if not rows:
                save("waiting_for_accounting", job)
                return
            if rows[0][1:3] != ["COMPLETED", "0:0"]:
                save("job_needs_repair", job)
                return
            receipt_path = Path(step["receipt"])
            if not receipt_path.is_file():
                save("missing_receipt_needs_repair", str(receipt_path))
                return
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("complete") is not True:
                save("incomplete_receipt_needs_repair", str(receipt_path))
                return
            # Audit programs must reconcile the exact preceding archive and its SHA256.
            if step["kind"] == "audit":
                previous = state["history"][-1]["receipt"]
                if (receipt.get("archive"), receipt.get("sha256")) != (
                    previous.get("archive"),
                    previous.get("sha256"),
                ):
                    raise ValueError(
                        "Audit does not identify preceding processing archive"
                    )
            else:
                if not receipt.get("archive") or not re.fullmatch(
                    r"[0-9a-f]{64}", receipt.get("sha256", "")
                ):
                    raise ValueError("Checksummed processing archive receipt required")
            state["history"].append({**active, "step": step, "receipt": receipt})
            state.pop("active")
            state["index"] += 1
            save("step_complete", job)
            return
        evidence = allocation()
        if (
            evidence["cpus"] + step["cpus"] > 47
            or evidence["memory_mib"] + step["cpus"] * step["mem_per_cpu_mib"] > 114000
        ):
            save("waiting_for_resources", evidence)
            return
        # Home only receives compact control receipts/logs. Reserving the large
        # scratch archive again against home would falsely stop larger sources.
        check(control, 16 * 1024**2, 20)
        check(step["stage"], step["reserve_bytes"], step["reserve_files"])
        name = "corpus-next-" + str(index) + "-" + digest[:10]
        if any(job["name"] == name for job in evidence["jobs"]):
            raise ValueError("Existing unrecorded matching job requires reconciliation")
        # Persist intent before contacting sbatch, so transport failures cannot duplicate jobs.
        state["active"] = {
            "job_name": name,
            "step_digest": digest,
            "intent_at": now(),
            "resources": evidence,
        }
        save("submitting", name)
        env = os.environ.copy()
        env.update(step.get("env", {}))
        env["LC_CODE_REVISION"] = step["code_revision"]
        args = [
            "sbatch",
            "--parsable",
            "--job-name=" + name,
            "--cpus-per-task=" + str(step["cpus"]),
            "--mem-per-cpu=" + str(step["mem_per_cpu_mib"]) + "M",
            "--time=" + step["time"],
            "--tmp=" + step["tmp"],
            "--output=" + step["log"],
            step["script"],
        ]
        result = (
            subprocess.check_output(args, text=True, timeout=45, env=env)
            .strip()
            .split(";", 1)[0]
        )
        if not result.isdigit():
            raise ValueError("Unrecognized submission response")
        state["active"].update(job_id=result, submitted_at=now())
        save("job_submitted", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--watch", type=int, help="seconds between bounded ticks")
    args = parser.parse_args()
    daemon_lock = None
    if args.watch:
        daemon_lock = (args.control / "controller-daemon.lock").open("a")
        fcntl.flock(daemon_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic(
            args.control / "controller-daemon.json",
            {"pid": os.getpid(), "started_at": now()},
        )
    while True:
        try:
            tick(args.control)
        except Exception as error:
            atomic(
                args.control / "controller-error.json",
                {"updated_at": now(), "error": repr(error)},
            )
            print(repr(error), flush=True)
            if not args.watch:
                raise
        if not args.watch or (args.control / "STOP_CONTROLLER").exists():
            return
        time.sleep(max(60, args.watch))


if __name__ == "__main__":
    main()
