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
    inspection.write_text(json.dumps(dict(reviewed=True, audit_job_id="41", sha256="b" * 64)))
    controller.tick(tmp_path)
    assert json.loads((tmp_path / "controller-state.json").read_text())["status"] == "waiting_for_agent_inspection"
    inspection.write_text(json.dumps(dict(reviewed=True, audit_job_id="42", sha256="c" * 64)))
    controller.tick(tmp_path)
    assert json.loads((tmp_path / "controller-state.json").read_text())["status"] == "waiting_for_agent_inspection"
    inspection.write_text(json.dumps(dict(reviewed=True, audit_job_id="42", sha256="b" * 64)))
    monkeypatch.setattr(controller, "allocation", lambda: dict(cpus=47, memory_mib=0, jobs=[]))
    controller.tick(tmp_path)
    assert json.loads((tmp_path / "controller-state.json").read_text())["status"] == "waiting_for_resources"
