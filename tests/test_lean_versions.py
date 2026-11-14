"""Axiom-policy checks shared by the supported historical Lean toolchains."""

import pytest

from lean_corpus.verify import check_problem, check_proof
from lean_corpus.worker import close_all


@pytest.mark.lean
@pytest.mark.parametrize("backend", ["process", "worker"])
def test_transitive_user_axiom_is_rejected(real_lean, backend):
    problem = {
        "id": "synthetic-transitive-axiom",
        "target_name": "target",
        "formal_statement": (
            "axiom unauthorized : True\n"
            "def intermediary : True := unauthorized\n"
            "opaque indirect : True := intermediary\n"
            "theorem target : True := by sorry"
        ),
    }
    runtime = {"runner": backend}
    try:
        baseline = check_problem(problem, real_lean, runtime)
        assert baseline["status"] == "elaborated", baseline
        result = check_proof(
            problem, {"formal_proof": "by exact indirect"}, real_lean, runtime, baseline
        )
        assert result["status"] == "invalid", result
        assert result["reason"] == "admission_or_unauthorized_axiom", result
        assert result["unauthorized_axioms"] == ["unauthorized"], result
    finally:
        close_all()
