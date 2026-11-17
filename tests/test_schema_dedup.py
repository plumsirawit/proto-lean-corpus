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


@pytest.mark.parametrize("reverse", [False, True])
def test_disk_merge_matches_existing_precedence_and_reports(tmp_path, reverse):
    from lean_corpus.disk_merge import merge_groups, parquet_rows

    a = problem(informal=None)
    b = problem(source="s2", informal="B")
    c = problem(informal="A")
    data = tables([a, b, c], [proof(a), proof(b, source="s2"), proof(a, "by sorry")])
    # Equal-reference precedence, null preference, multiple attempts, Unicode,
    # duplicate provenance and timestamp types must retain existing semantics.
    data["problems"][1]["informal_statement"] = "B ∀"
    data["provenance"].append(dict(data["provenance"][0]))
    if reverse:
        data = {k: list(reversed(v)) for k, v in data.items()}
    expected, expected_report = merge(data)
    write_tables(tmp_path / "input", data, unique=False)
    actual = {k: [] for k in SCHEMAS}
    report = {"duplicate_groups": [], "metadata_conflicts": [],
              "before": dict.fromkeys(SCHEMAS, 0), "after": dict.fromkeys(SCHEMAS, 0)}
    for name, row, fragment in merge_groups(
        parquet_rows(tmp_path / "input", batch_size=1), tmp_path / "merge.sqlite"
    ):
        actual[name].append(row)
        for field in ("duplicate_groups", "metadata_conflicts"):
            report[field].extend(fragment[field])
        for field in ("before", "after"):
            report[field][name] += fragment[field][name]
    assert actual == expected
    assert report == expected_report
    validate(actual)


def test_disk_merge_oversized_group_preserves_all_occurrences(tmp_path):
    import sqlite3
    from lean_corpus.disk_merge import merge_groups

    p = problem()
    database = tmp_path / "merge.sqlite"
    with pytest.raises(ValueError, match="all occurrences retained"):
        list(merge_groups((("problems", p) for _ in range(3)), database,
                          max_group_bytes=1))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 3
    with pytest.raises(FileExistsError):
        list(merge_groups([], database))


def test_disk_merge_empty_and_input_failure(tmp_path):
    import sqlite3
    from lean_corpus.disk_merge import merge_groups

    assert list(merge_groups([], tmp_path / "empty.sqlite")) == []
    database = tmp_path / "bad.sqlite"
    with pytest.raises(ValueError, match="unknown table"):
        list(merge_groups([("unexpected", {})], database))
    # Failure closes the connection, and a later attempt cannot reuse the DB.
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("unique", [False, True])
def test_disk_validation_matches_memory_and_forward_references(tmp_path, unique):
    from lean_corpus.disk_schema import validate_occurrences, validate_parquet

    p = problem()
    data = tables([p], [proof(p), proof(p, "by sorry")])
    if not unique:
        data["problems"].append(dict(p))
        data["provenance"].append(dict(data["provenance"][0]))
    validate(data, unique=unique)
    expected = {k: len(v) for k, v in data.items()}
    # Proofs precede their problem and all source references precede provenance.
    occurrences = ((name, row) for name in ("proofs", "problems", "provenance")
                   for row in data[name])
    assert validate_occurrences(occurrences, tmp_path / "stream.db", unique) == expected
    write_tables(tmp_path / "input", data, unique=unique)
    assert validate_parquet(tmp_path / "input", tmp_path / "parquet.db", unique,
                            batch_size=1) == expected
    with pytest.raises(FileExistsError):
        validate_parquet(tmp_path / "input", tmp_path / "parquet.db", unique)


@pytest.mark.parametrize("fault, message", [
    ("duplicate", "duplicate problems id"),
    ("null", "problems.formal_statement cannot be null"),
    ("empty", "problems.target_name cannot be empty"),
    ("refs", "empty source_refs"),
    ("location", "invalid source reference"),
    ("license", "invalid license_refs"),
    ("status", "invalid verification status"),
    ("problem", "dangling problem foreign key"),
    ("source", "dangling provenance foreign key"),
])
def test_disk_validation_preserves_all_existing_rejections(tmp_path, fault, message):
    from lean_corpus.disk_schema import validate_occurrences

    p = problem()
    data = tables([p], [proof(p)])
    if fault == "duplicate":
        data["problems"].append(dict(p))
    elif fault == "null":
        p["formal_statement"] = None
    elif fault == "empty":
        p["target_name"] = "  "
    elif fault == "refs":
        p["source_refs"] = []
    elif fault == "location":
        p["source_refs"][0]["location"] = ""
    elif fault == "license":
        data["provenance"][0]["license_refs"] = [None]
    elif fault == "status":
        data["proofs"][0]["verification_status"] = "upstream_valid"
    elif fault == "problem":
        data["proofs"][0]["problem_id"] = "missing"
    elif fault == "source":
        data["proofs"][0]["source_refs"][0]["source_id"] = "missing"
    with pytest.raises(ValueError, match=message):
        validate(data)
    database = tmp_path / "rejection.db"
    with pytest.raises(ValueError, match=message):
        validate_occurrences(((k, r) for k, rows in data.items() for r in rows),
                             database)
    assert database.exists()


def test_disk_validation_rejects_schema_and_iterator_failures(tmp_path):
    import pyarrow as pa
    from lean_corpus.disk_schema import validate_occurrences, validate_parquet

    data = tables([], [])
    write_tables(tmp_path / "input", data)
    pq.write_table(pa.table({"id": ["wrong schema"]}),
                   tmp_path / "input" / "problems.parquet")
    with pytest.raises(ValueError, match="unexpected problems schema"):
        validate_parquet(tmp_path / "input", tmp_path / "schema.db")

    def broken():
        yield "problems", problem()
        raise RuntimeError("incomplete input")

    with pytest.raises(RuntimeError, match="incomplete input"):
        validate_occurrences(broken(), tmp_path / "broken.db")
    assert validate_occurrences(iter(()), tmp_path / "empty.db") == dict.fromkeys(SCHEMAS, 0)


@pytest.mark.parametrize("fault", ["metadata", "child_nullable", "field_name",
                                  "field_type", "top_nullable"])
def test_disk_parquet_schema_retains_nested_contract(tmp_path, fault):
    import pyarrow as pa
    from lean_corpus.disk_merge import parquet_rows

    data = tables([], [])
    write_tables(tmp_path / "input", data)
    schema = SCHEMAS["problems"]
    if fault == "metadata":
        schema = schema.with_metadata({b"lean_corpus_schema_version": b"wrong"})
    elif fault == "top_nullable":
        schema = schema.set(0, schema.field(0).with_nullable(True))
    else:
        ref = schema.field("source_refs").type.value_field
        if fault == "child_nullable":
            ref = ref.with_nullable(True)
        else:
            fields = list(ref.type)
            if fault == "field_name":
                fields[0] = fields[0].with_name("wrong_source_id")
            else:
                fields[0] = fields[0].with_type(pa.int64())
            ref = ref.with_type(pa.struct(fields))
        index = schema.get_field_index("source_refs")
        schema = schema.set(index, schema.field(index).with_type(pa.list_(ref)))
    pq.write_table(pa.Table.from_batches([], schema=schema),
                   tmp_path / "input" / "problems.parquet")
    with pytest.raises(ValueError, match="unexpected problems schema"):
        list(parquet_rows(tmp_path / "input"))
