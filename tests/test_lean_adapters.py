from pathlib import Path
import pytest
from lean_corpus.adapters.records import (
    ADAPTERS,
    Unsupported,
    code_block,
    proof_term,
)
from lean_corpus.lean import target_info
from lean_corpus.verify import cache_key, check_problem, check_proof

pytestmark = pytest.mark.lean
TASK = "theorem t : True := by sorry"


def test_structural_boundaries(real_lean):
    source = 'def text := "sorry"\n/- sorry /- sorry -/ -/\n-- sorry\n' + TASK
    assert target_info(source, real_lean)["name"] == "t"
    for bad in (
        "def x : Nat := by sorry\n" + TASK,
        "theorem t : True := by\n  have : True := by sorry\n  sorry",
    ):
        with pytest.raises(ValueError):
            target_info(bad, real_lean)
    with pytest.raises(Unsupported, match="changed"):
        proof_term("theorem t : False := by sorry", TASK, real_lean, {})
    with pytest.raises(Unsupported, match="ambiguous"):
        code_block("```lean\nby sorry\n```\n```lean\nby trivial\n```")


def test_verification_boundaries(real_lean):
    problem = {"id": "p", "formal_statement": TASK, "target_name": "t"}
    baseline = check_problem(problem, real_lean, {})
    assert baseline["status"] == "elaborated", baseline
    for term, expected in (
        ("by trivial", "valid"),
        ("by exact Nat.zero", "invalid"),
        ("by sorry", "invalid"),
        ("by admit", "invalid"),
        ("by trivial\naxiom stolen : False", "invalid"),
    ):
        result = check_proof(problem, {"formal_proof": term}, real_lean, {}, baseline)
        assert result["status"] == expected, result
    unresolved = {**real_lean, "resolved": False}
    assert check_problem(problem, unresolved, {})["status"] == "inconclusive"
    a = cache_key(problem, {"formal_proof": "by trivial"}, real_lean)
    assert a != cache_key(problem, {"formal_proof": "by trivial"}, unresolved)
    assert a != cache_key(
        problem, {"formal_proof": "by trivial"}, real_lean, {"version": "changed"}
    )


def test_full_response_context_shape(real_lean):
    response = "/- supplied annotation -/\n\n" + TASK.replace("by sorry", "by trivial")
    assert proof_term(response, TASK, real_lean, {}) == "by trivial"
    for prefix in ("import Lean\n", "set_option autoImplicit false\n"):
        with pytest.raises(Unsupported, match="changed"):
            proof_term(prefix + response, TASK, real_lean, {})


def test_source_forms(real_lean):
    for key in (
        "goedel_pset_v1",
        "lean_workbook",
        "numina_math_lean",
        "putnam_bench",
        "nemotron_math_proofs_v1",
    ):
        row = {
            "formal_statement": TASK,
            "formal_ground_truth": "",
            "formal_proof": None,
            "ground_truth_type": "statement_only",
            "messages": [],
        }
        result = ADAPTERS[key](row, real_lean, {})
        assert result["task"] == TASK
        assert not result["attempts"]
    workbook = ADAPTERS["lean_workbook"](
        {"formal_statement": TASK, "tactic": "trivial", "status": "proved"},
        real_lean,
        {},
    )
    assert workbook["attempts"][0]["proof"] == "by\n  trivial"
    numina = ADAPTERS["numina_math_lean"](
        {
            "formal_statement": "theorem t : True := by",
            "formal_ground_truth": "by sorry",
            "formal_proof": "by trivial",
            "ground_truth_type": "complete",
        },
        real_lean,
        {},
    )
    assert len(numina["attempts"]) == 2
    with pytest.raises(ValueError, match="complete supporting"):
        ADAPTERS["putnam_bench"](
            {"lean": "def answer : Nat := by sorry\n" + TASK}, real_lean, {}
        )
    messages = [
        {"role": "user", "content": f"Complete\n```lean4\n{TASK}\n```"},
        {"role": "assistant", "content": "```lean4\nby sorry\n```"},
        {"role": "tool", "content": "error"},
        {"role": "assistant", "content": "```lean4\nby trivial\n```"},
    ]
    sft = ADAPTERS["goedel_prover_v2_sft"]({"messages": messages}, real_lean, {})
    assert [a["field"] for a in sft["attempts"]] == [
        "messages/1/content",
        "messages/3/content",
    ]
    with pytest.raises(Unsupported, match="missing formalization"):
        ADAPTERS["nemotron_math_proofs_v1"]({"formal_statement": None}, real_lean, {})


def test_library_context(real_lean):
    path = Path("tests/fixtures/library/Example.lean")
    results = list(ADAPTERS["mathlib4"](path, real_lean, {}))
    assert results == list(ADAPTERS["tau_ceti"](path, real_lean, {}))
    assert len(results) == 2
    assert all("error" not in r for r in results), results
    assert 'def label := "sorry is just text"' in results[1]["task"]
    assert "theorem first : n = n := by rfl" in results[1]["task"]
    assert results[1]["task"].endswith("end Fixture\n")
    problem = {
        "id": "lib",
        "formal_statement": results[1]["task"],
        "target_name": results[1]["target"],
    }
    base = check_problem(problem, real_lean, {})
    assert (
        check_proof(problem, {"formal_proof": "by trivial"}, real_lean, {}, base)[
            "status"
        ]
        == "valid"
    )


def test_local_syntax_and_named_scopes(real_lean):
    results = list(
        ADAPTERS["mathlib4"](
            Path("tests/fixtures/library/LocalSyntax.lean"), real_lean, {}
        )
    )
    assert len(results) == 1 and "error" not in results[0], results
    result = results[0]
    assert result["target"] == "Nested.Inner.local_syntax"
    assert result["task"].endswith("end Inner\nend Nested\nend Named\n")
    problem = {
        "id": "local-syntax",
        "formal_statement": result["task"],
        "target_name": result["target"],
    }
    baseline = check_problem(problem, real_lean, {})
    verified = check_proof(problem, {"formal_proof": "by rfl"}, real_lean, {}, baseline)
    assert verified["status"] == "valid", verified


def test_axiom_dependencies(real_lean):
    task = "axiom unsupported : False\ntheorem t : True := by sorry"
    problem = {"id": "axiom", "formal_statement": task, "target_name": "t"}
    baseline = check_problem(problem, real_lean, {})
    result = check_proof(
        problem,
        {"formal_proof": "by exact False.elim unsupported"},
        real_lean,
        {},
        baseline,
    )
    assert result["status"] == "invalid", result
    assert "unsupported" in result["unauthorized_axioms"]


def test_infrastructure_classification(real_lean, monkeypatch):
    from lean_corpus.lean import LeanFailure

    problem = {"id": "infra", "formal_statement": TASK, "target_name": "t"}
    for reason in (
        "timeout",
        "resource_exhaustion",
        "toolchain_unavailable",
        "isolation_unavailable",
    ):

        def unavailable(*args, **kwargs):
            raise LeanFailure(reason)

        monkeypatch.setattr("lean_corpus.verify.inspect", unavailable)
        # Synthetic failure injection tests classification only, never kernel validity.
        result = check_proof(
            problem,
            {"formal_proof": "by trivial"},
            real_lean,
            {},
            {"status": "elaborated", "target": {"body": [20, 28]}, "type": "True"},
        )
        assert result["status"] == "inconclusive" and result["reason"] == reason


def test_lean_internal_resource_budget(real_lean):
    from lean_corpus.lean import LeanFailure, inspect

    with pytest.raises(LeanFailure) as error:
        inspect(
            "set_option maxRecDepth 1\ntheorem budget : True := by trivial",
            real_lean,
            mode="verify",
            target="budget",
        )
    assert error.value.reason == "resource_exhaustion"
