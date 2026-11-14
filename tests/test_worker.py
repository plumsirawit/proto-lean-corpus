import time
import pytest
from lean_corpus.lean import LeanFailure, inspect
from lean_corpus.verify import check_problem, check_proof
from lean_corpus.worker import STATS, close_all

pytestmark = pytest.mark.lean
TASK = "theorem t : True := by sorry"


@pytest.fixture
def worker_runtime(real_lean):
    yield {"runner": "worker", "worker_max_cases": 32}
    close_all()


def test_worker_fresh_context_and_kernel_checks(real_lean, worker_runtime):
    first = "def leaked : Nat := 0\n" + TASK.replace("by sorry", "by trivial")
    assert not inspect(first, real_lean, worker_runtime, "verify", "t")["errors"]
    second = inspect(
        TASK.replace("by sorry", "by exact leaked"),
        real_lean,
        worker_runtime,
        "verify",
        "t",
    )
    assert any(
        "unknown identifier" in error and "leaked" in error
        for error in second["errors"]
    )
    option = "set_option autoImplicit false\n" + TASK.replace("by sorry", "by trivial")
    assert not inspect(option, real_lean, worker_runtime, "verify", "t")["errors"]
    independent = "theorem t (x : α) : x = x := by rfl"
    assert not inspect(independent, real_lean, worker_runtime, "verify", "t")["errors"]
    problem = {"id": "worker", "formal_statement": TASK, "target_name": "t"}
    baseline = check_problem(problem, real_lean, worker_runtime)
    for term, expected in (
        ("by trivial", "valid"),
        ("by sorry", "invalid"),
        ("by exact Nat.zero", "invalid"),
    ):
        assert (
            check_proof(
                problem, {"formal_proof": term}, real_lean, worker_runtime, baseline
            )["status"]
            == expected
        )


def test_worker_header_change_and_bounded_lifetime(real_lean, worker_runtime):
    worker_runtime["worker_max_cases"] = 2
    before = dict(STATS)
    source = TASK.replace("by sorry", "by trivial")
    assert not inspect(source, real_lean, worker_runtime, "verify", "t")["errors"]
    source = "import Lean\n" + source
    for _ in range(3):
        assert not inspect(source, real_lean, worker_runtime, "verify", "t")["errors"]
    assert STATS["header_restarts"] > before["header_restarts"]
    assert STATS["starts"] >= before["starts"] + 3


def test_worker_timeout_restarts_cleanly(real_lean, worker_runtime):
    warm = "import Lean\n" + TASK.replace("by sorry", "by trivial")
    assert not inspect(warm, real_lean, worker_runtime, "verify", "t")["errors"]
    slow = "import Lean\n" + TASK.replace("by sorry", "by\n  sleep 10000\n  trivial")
    started = time.monotonic()
    with pytest.raises(LeanFailure) as error:
        inspect(slow, real_lean, worker_runtime, "verify", "t", timeout=1)
    assert error.value.reason == "timeout"
    assert time.monotonic() - started < 8
    assert not inspect(warm, real_lean, worker_runtime, "verify", "t")["errors"]


def test_worker_metaprogramming_falls_back_read_only(
    real_lean, worker_runtime, tmp_path
):
    guard = tmp_path / "guard.txt"
    guard.write_text("preserved")
    worker_runtime.update(project_dir=str(tmp_path))
    before = STATS["fallbacks"]
    source = (
        'import Lean\n#eval IO.FS.writeFile "guard.txt" "changed"\n'
        + TASK.replace("by sorry", "by trivial")
    )
    result = inspect(source, real_lean, worker_runtime, "verify", "t")
    assert result["errors"]
    assert guard.read_text() == "preserved"
    assert STATS["fallbacks"] == before + 1

    custom = (
        "import Lean\nopen Lean Elab Tactic\n"
        'elab "write_guard" : tactic => do\n'
        '  IO.FS.writeFile "guard.txt" "changed"\n'
        "  evalTactic (← `(tactic| trivial))\n"
        + TASK.replace("by sorry", "by write_guard")
    )
    result = inspect(custom, real_lean, worker_runtime, "verify", "t")
    assert result["errors"]
    assert guard.read_text() == "preserved"
    assert STATS["fallbacks"] == before + 2


def test_worker_masks_proof_metaprogramming_only_during_parse(
    real_lean, worker_runtime, tmp_path
):
    guard = tmp_path / "guard.txt"
    guard.write_text("preserved")
    worker_runtime["project_dir"] = str(tmp_path)
    source = (
        "import Lean\ntheorem t : True := by\n"
        '  run_tac do IO.FS.writeFile "guard.txt" "changed"\n  trivial'
    )
    before = STATS["fallbacks"]
    parsed = inspect(source, real_lean, worker_runtime)
    assert not parsed["errors"] and len(parsed["targets"]) == 1
    assert parsed["admissions"] == 0  # original syntax retained
    assert STATS["fallbacks"] == before
    assert guard.read_text() == "preserved"
    result = inspect(source, real_lean, worker_runtime, "verify", "t")
    assert any("read-only" in error.lower() for error in result["errors"])
    assert STATS["fallbacks"] == before + 1
    assert guard.read_text() == "preserved"
    assert STATS["peak_vm_size_kib"] >= STATS["peak_rss_kib"] > 0
