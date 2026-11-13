from datetime import datetime, timezone
import pytest
import pyarrow.parquet as pq
from lean_corpus.common import identity
from lean_corpus.schema import REF, SCHEMAS, read_tables, validate, write_tables
from lean_corpus.deduplicate import merge


def problem(
    statement="theorem t : True := by sorry",
    env="env",
    source="s1",
    upstream="same",
    informal=None,
):
    pid = identity("problem", {"source": statement, "target": "t", "environment": env})
    return dict(
        id=pid,
        formal_statement=statement,
        informal_statement=informal,
        target_name="t",
        environment_id=env,
        source_refs=[
            dict(
                source_id=source,
                upstream_id=upstream,
                location="data.json#row=0&field=formal_statement",
            )
        ],
    )


def proof(p, term="by trivial", source="s1"):
    return dict(
        id=identity("proof", {"problem": p["id"], "term": term}),
        problem_id=p["id"],
        formal_proof=term,
        informal_proof=None,
        verification_status="unverified",
        source_refs=[
            dict(
                source_id=source,
                upstream_id="same",
                location="data.json#row=0&field=proof",
            )
        ],
    )


def tables(problems, proofs):
    return {
        "problems": problems,
        "proofs": proofs,
        "provenance": [
            dict(
                id=s,
                dataset_name=s,
                revision="revision",
                acquired_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                manifest_hash="hash",
                license_refs=[],
            )
            for s in ("s1", "s2")
        ],
    }


def test_duplicate_union_and_multiple_attempts(tmp_path):
    p1, p2 = problem(informal="A"), problem(source="s2", informal="B")
    p3 = problem("theorem t : False := by sorry", source="s2")
    p4 = problem(env="other-env")
    data, report = merge(
        tables(
            [p1, p2, p3, p4],
            [
                proof(p1),
                proof(p2, source="s2"),
                proof(p1, "by sorry"),
                proof(p3),
                proof(p4),
            ],
        )
    )
    validate(data)
    assert len(data["problems"]) == 3
    assert len(data["proofs"]) == 4
    merged = next(p for p in data["problems"] if p["id"] == p1["id"])
    assert {r["source_id"] for r in merged["source_refs"]} == {"s1", "s2"}
    assert report["metadata_conflicts"]
    write_tables(tmp_path, data)
    assert read_tables(tmp_path) == data
    assert pq.read_schema(tmp_path / "problems.parquet") == SCHEMAS["problems"]
    assert SCHEMAS["problems"].field("source_refs").type.value_type == REF
    assert "proof_ids" not in SCHEMAS["problems"].names


def test_invariants():
    p = problem()
    with pytest.raises(ValueError, match="foreign key"):
        validate(tables([], [proof(p)]))
    p["source_refs"] = []
    with pytest.raises(ValueError, match="source_refs"):
        validate(tables([p], []))
    p = problem(statement="")
    with pytest.raises(ValueError, match="empty"):
        validate(tables([p], []))


def test_order_independent():
    a, b = problem(informal="A"), problem(source="s2", informal="B")
    x = tables([a, b], [proof(a), proof(b, source="s2")])
    y = tables([b, a], [proof(b, source="s2"), proof(a)])
    assert merge(x) == merge(y)


def test_provenance_coverage_and_attempt_location():
    p = problem(source="unknown")
    with pytest.raises(ValueError, match="provenance"):
        validate(tables([p], []))
    p = problem()
    a, b = proof(p), proof(p)
    b["source_refs"][0]["location"] = "data.json#row=0&field=messages/3/content"
    data, _ = merge(tables([p], [a, b]))
    assert len(data["proofs"]) == 1
    assert len(data["proofs"][0]["source_refs"]) == 2
