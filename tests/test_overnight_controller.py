"""The unattended submitter must fail closed on caps, gates and uncertain replies."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
spec = importlib.util.spec_from_file_location(
    "overnight_controller",
    Path(__file__).resolve().parents[1] / "scripts/overnight_controller.py",
)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def step(kind="process", size=10):
    return dict(
        source="goedel_prover_v2_sft",
        size=size,
        kind=kind,
        cpus=2,
        mem_per_cpu_mib=2048,
        code_revision="a" * 40,
        reserve_bytes=1024,
        reserve_files=10,
        independent_audit=kind == "audit",
        stage="/scratch/example",
        receipt="/nonexistent/receipt.json",
        script="/example/script",
        log="/example/log",
        time="00:10:00",
        tmp="4G",
    )


def test_cap_and_required_audit_order():
    assert controller.validate_plan({"steps": [step(), step("audit")]})
    with pytest.raises(ValueError, match="gates"):
        controller.validate_plan({"steps": [step(), step(size=100)]})
    oversized = step()
    oversized.update(cpus=47, mem_per_cpu_mib=8192)
    with pytest.raises(ValueError, match="memory cap"):
        controller.validate_plan({"steps": [oversized]})
    assert controller.memory_mib("4G") == 4096
    assert controller.memory_mib("114000M") == 114000
    with pytest.raises(ValueError):
        controller.memory_mib("unknown")


def test_active_requested_memory_is_counted(monkeypatch):
    monkeypatch.setenv("USER", "test-user")

    def command(args):
        if args[0] == "squeue":
            return "42|other-work"
        if args[0] == "scontrol":
            return "JobId=42 ReqTRES=cpu=16,mem=90G,node=1"
        return "account|normal|"

    monkeypatch.setattr(controller, "command", command)
    actual = controller.allocation()
    assert (actual["cpus"], actual["memory_mib"]) == (16, 92160)


def test_resource_wait_never_submits(tmp_path, monkeypatch):
    (tmp_path / "controller-plan.json").write_text(json.dumps({"steps": [step()]}))
    monkeypatch.setattr(
        controller, "allocation", lambda: dict(cpus=46, memory_mib=0, jobs=[])
    )
    monkeypatch.setattr(
        controller, "check", lambda *a: pytest.fail("must wait before quota writes")
    )
    controller.tick(tmp_path)
    state = json.loads((tmp_path / "controller-state.json").read_text())
    assert state["status"] == "waiting_for_resources"
    assert "active" not in state


def test_uncertain_submission_cannot_retry(tmp_path, monkeypatch):
    (tmp_path / "controller-plan.json").write_text(json.dumps({"steps": [step()]}))
    monkeypatch.setattr(
        controller, "allocation", lambda: dict(cpus=0, memory_mib=0, jobs=[])
    )
    monkeypatch.setattr(controller, "check", lambda *a: None)
    calls = []

    def failed(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("scheduler accepted but response lost")

    monkeypatch.setattr(controller.subprocess, "check_output", failed)
    with pytest.raises(TimeoutError):
        controller.tick(tmp_path)
    controller.tick(tmp_path)
    state = json.loads((tmp_path / "controller-state.json").read_text())
    assert state["status"] == "submission_needs_reconciliation"
    assert len(calls) == 1


def test_reservations_follow_the_destination_filesystem(tmp_path, monkeypatch):
    planned = step()
    planned["reserve_bytes"] = 8 * 1024**3
    (tmp_path / "controller-plan.json").write_text(json.dumps({"steps": [planned]}))
    monkeypatch.setattr(
        controller, "allocation", lambda: dict(cpus=0, memory_mib=0, jobs=[])
    )
    reservations = []
    monkeypatch.setattr(controller, "check", lambda *args: reservations.append(args))
    monkeypatch.setattr(controller.subprocess, "check_output", lambda *a, **k: "123\n")
    controller.tick(tmp_path)
    assert reservations == [
        (tmp_path, 16 * 1024**2, 20),
        (planned["stage"], 8 * 1024**3, planned["reserve_files"]),
    ]
    assert (
        json.loads((tmp_path / "controller-state.json").read_text())["active"]["job_id"]
        == "123"
    )


def test_next_size_requires_agent_inspection(tmp_path, monkeypatch):
    first, audit, next_size = step(), step("audit"), step(size=100)
    (tmp_path / "controller-plan.json").write_text(
        json.dumps({"steps": [first, audit, next_size]})
    )
    state = {
        "index": 2,
        "history": [
            {
                "job_id": "41",
                "step": first,
                "receipt": {"archive": "/result.tar", "sha256": "b" * 64},
            },
            {
                "job_id": "42",
                "step": audit,
                "receipt": {"archive": "/result.tar", "sha256": "b" * 64},
            },
        ],
    }
    (tmp_path / "controller-state.json").write_text(json.dumps(state))
    monkeypatch.setattr(
        controller, "allocation", lambda: pytest.fail("inspection must happen first")
    )
    controller.tick(tmp_path)
    saved = json.loads((tmp_path / "controller-state.json").read_text())
    assert saved["status"] == "waiting_for_agent_inspection"
    assert saved["index"] == 2
    inspection = tmp_path / "inspection-42.json"
    inspection.write_text(
        json.dumps(dict(reviewed=True, audit_job_id="41", sha256="b" * 64))
    )
    controller.tick(tmp_path)
    assert (
        json.loads((tmp_path / "controller-state.json").read_text())["status"]
        == "waiting_for_agent_inspection"
    )
    inspection.write_text(
        json.dumps(dict(reviewed=True, audit_job_id="42", sha256="c" * 64))
    )
    controller.tick(tmp_path)
    assert (
        json.loads((tmp_path / "controller-state.json").read_text())["status"]
        == "waiting_for_agent_inspection"
    )
    inspection.write_text(
        json.dumps(dict(reviewed=True, audit_job_id="42", sha256="b" * 64))
    )
    monkeypatch.setattr(
        controller, "allocation", lambda: dict(cpus=47, memory_mib=0, jobs=[])
    )
    controller.tick(tmp_path)
    assert (
        json.loads((tmp_path / "controller-state.json").read_text())["status"]
        == "waiting_for_resources"
    )


def test_independent_source_lane_still_requires_size_audit_order():
    first, audit = step(), step("audit")
    for item in (first, audit):
        item["source"] = "nemotron_math_proofs_v1"
    plan = dict(source_lane="nemotron_math_proofs_v1", steps=[first, audit])
    assert controller.validate_plan(plan) == [first, audit]
    with pytest.raises(ValueError, match="gates"):
        controller.validate_plan({**plan, "steps": [audit, first]})
    with pytest.raises(ValueError, match="Unknown source"):
        controller.validate_plan({**plan, "source_lane": "unrelated"})


def test_adopted_job_passes_accounting_and_receipt_gate_without_submission(
    tmp_path, monkeypatch
):
    planned = step()
    planned.update(adopt_job_id="123", receipt=str(tmp_path / "receipt.json"))
    (tmp_path / "controller-plan.json").write_text(json.dumps({"steps": [planned]}))
    monkeypatch.setattr(
        controller, "allocation", lambda: pytest.fail("adoption must never submit")
    )
    controller.tick(tmp_path)
    monkeypatch.setattr(
        controller,
        "command",
        lambda args: "" if args[0] == "squeue" else "123|FAILED|1:0||",
    )
    controller.tick(tmp_path)
    assert (
        json.loads((tmp_path / "controller-state.json").read_text())["status"]
        == "job_needs_repair"
    )
    monkeypatch.setattr(
        controller,
        "command",
        lambda args: "" if args[0] == "squeue" else "123|COMPLETED|0:0||",
    )
    (tmp_path / "receipt.json").write_text(
        json.dumps(dict(complete=True, archive="/result.tar", sha256="b" * 64))
    )
    controller.tick(tmp_path)
    saved = json.loads((tmp_path / "controller-state.json").read_text())
    assert saved["index"] == 1 and saved["history"][0]["job_id"] == "123"


def test_lane_root_lock_excludes_external_submission_and_fault_isolation(
    tmp_path, monkeypatch
):
    import fcntl

    for source in controller.SOURCES[:2]:
        lane = tmp_path / "lanes" / source
        lane.mkdir(parents=True)
        (lane / "controller-plan.json").write_text(
            json.dumps(dict(source_lane=source, steps=[]))
        )
    visited = []

    def fake_tick(lane):
        with (tmp_path / "controller.lock").open("a") as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        visited.append(lane.name)
        if len(visited) == 1:
            raise TimeoutError("first source accounting unavailable")

    monkeypatch.setattr(controller, "tick", fake_tick)
    controller.tick_lanes(tmp_path)
    assert visited == list(controller.SOURCES[:2])
    assert (
        "TimeoutError"
        in (
            tmp_path / "lanes" / controller.SOURCES[0] / "controller-error.json"
        ).read_text()
    )
