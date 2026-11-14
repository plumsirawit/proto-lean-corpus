from pathlib import Path
import pytest
from lean_corpus.acquire import acquire, check_snapshot, project_environment
from lean_corpus.common import identity, read_json
from lean_corpus.environment import environment_for, resolve_environment
from lean_corpus.verify import validate_runtime


def test_checker_respects_elan_home(monkeypatch, tmp_path, core_env):
    from lean_corpus.lean import LeanFailure, inspect

    monkeypatch.setenv("ELAN_HOME", str(tmp_path))
    with pytest.raises(LeanFailure) as error:
        inspect("", core_env)
    assert error.value.reason == "toolchain_unavailable"


def test_resolution_is_explicit_and_changes_identity(tmp_path):
    work = tmp_path / "work"
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "data.jsonl").write_text(
        '{"formal_statement":"theorem t : True := by sorry"}\n'
    )
    sid = acquire(work, "configs/sources.yaml", "numina_math_lean", raw)
    manifest = check_snapshot(work / "snapshots" / sid)
    old_id = manifest["environment_id"]
    assert not read_json(work / "environments" / f"{old_id}.json")["resolved"]
    project = Path("tests/fixtures/numina")
    result = resolve_environment(work, sid, project)
    new_id = result["environment_id"]
    assert old_id != new_id
    assert check_snapshot(work / "snapshots" / sid) == manifest
    assert environment_for(work, sid, manifest) == new_id
    assert (work / "environment-projects" / new_id / "lean-toolchain").is_file()
    env = read_json(work / "environments" / f"{new_id}.json")
    validate_runtime(env, {"project_dir": str(project)})
    assert identity("environment", env) == new_id


def test_unknown_environments_do_not_alias(tmp_path):
    assert project_environment(tmp_path, "source_a") != project_environment(
        tmp_path, "source_b"
    )
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
    (tmp_path / "lakefile.toml").write_text('name = "fixture"\n')
    env = project_environment(tmp_path, "source_a")
    assert not env["resolved"]
    with pytest.raises(ValueError, match="runtime (source|configuration) differs"):
        validate_runtime(
            {**env, "resolved": True}, {"project_dir": "/tmp/does-not-exist"}
        )
