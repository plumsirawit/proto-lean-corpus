import os
import pytest
from lean_corpus.lean import inspect, LeanFailure


@pytest.fixture(scope="session")
def core_env():
    return {
        "schema_version": "1",
        "toolchain": os.environ.get(
            "LEAN_CORPUS_TEST_TOOLCHAIN", "leanprover/lean4:v4.19.0"
        ),
        "packages": [],
        "build_config": {},
        "resolved": True,
        "hints": {},
    }


@pytest.fixture(scope="session")
def real_lean(core_env):
    try:
        result = inspect(
            "theorem probe : True := by trivial",
            core_env,
            mode="verify",
            target="probe",
        )
    except LeanFailure as e:
        if os.environ.get("LEAN_CORPUS_REQUIRE_LEAN") == "1":
            pytest.fail(f"required real Lean environment unavailable: {e}")
        pytest.skip(f"real Lean environment unavailable: {e}")
    assert not result["errors"]
    return core_env
