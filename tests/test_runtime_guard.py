"""Runtime freezing and bounded validation, without simulated kernel results."""

import pytest
from lean_corpus.acquire import project_environment
from lean_corpus.common import identity, write_json
from lean_corpus.runtime import RuntimeGuard
import lean_corpus.runtime as module


def test_stage_validates_once_and_rechecks_inputs(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    toolchain = project / "lean-toolchain"
    toolchain.write_text("leanprover/lean4:v4.19.0\n")
    environment = project_environment(project, "test")
    eid = identity("environment", environment)
    work = tmp_path / "work"
    write_json(work / "environments" / f"{eid}.json", environment)
    mapping = work / "runtimes" / f"{eid}.json"
    write_json(mapping, {"project_dir": str(project)})
    calls = []
    original = module.validate_runtime

    def validate(env, runtime):
        calls.append(env)
        original(env, runtime)

    monkeypatch.setattr(module, "validate_runtime", validate)
    guard = RuntimeGuard(work)
    for _ in range(100):
        assert guard.get(eid)[0] == environment
    assert len(calls) == 1
    guard.finish()
    assert len(calls) == 2
    toolchain.write_text("changed\n")
    with pytest.raises(ValueError, match="runtime source differs"):
        guard.finish()
    toolchain.write_text("leanprover/lean4:v4.19.0\n")
    write_json(mapping, {"project_dir": str(project), "runner": "worker"})
    with pytest.raises(ValueError, match="changed during stage"):
        guard.finish()
    environment["toolchain"] = "different"
    write_json(work / "environments" / f"{eid}.json", environment)
    with pytest.raises(ValueError, match="identity mismatch"):
        RuntimeGuard(work).get(eid)
