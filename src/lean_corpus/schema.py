from datetime import datetime
from pathlib import Path
import os
import pyarrow as pa
import pyarrow.parquet as pq
from .common import VERSION

REF = pa.struct(
    [
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("upstream_id", pa.string()),
        pa.field("location", pa.string(), nullable=False),
    ]
)
REFS = pa.list_(pa.field("item", REF, nullable=False))


def schema(fields):
    return pa.schema(fields, metadata={b"lean_corpus_schema_version": VERSION.encode()})


SCHEMAS = {
    "problems": schema(
        [
            pa.field("id", pa.string(), False),
            pa.field("formal_statement", pa.string(), False),
            pa.field("informal_statement", pa.string()),
            pa.field("target_name", pa.string(), False),
            pa.field("environment_id", pa.string(), False),
            pa.field("source_refs", REFS, False),
        ]
    ),
    "proofs": schema(
        [
            pa.field("id", pa.string(), False),
            pa.field("problem_id", pa.string(), False),
            pa.field("formal_proof", pa.string(), False),
            pa.field("informal_proof", pa.string()),
            pa.field("verification_status", pa.string(), False),
            pa.field("source_refs", REFS, False),
        ]
    ),
    "provenance": schema(
        [
            pa.field("id", pa.string(), False),
            pa.field("dataset_name", pa.string(), False),
            pa.field("revision", pa.string(), False),
            pa.field("acquired_at", pa.timestamp("us", tz="UTC"), False),
            pa.field("manifest_hash", pa.string(), False),
            pa.field(
                "license_refs", pa.list_(pa.field("item", pa.string(), False)), False
            ),
        ]
    ),
}


def validate_rows(name, rows, unique=True):
    """Validate row invariants; callers separately enforce cross-table keys."""
    ids = set()
    for row in rows:
        for field in SCHEMAS[name]:
            value = row.get(field.name)
            if not field.nullable and value is None:
                raise ValueError(f"{name}.{field.name} cannot be null")
            if isinstance(value, str) and not field.nullable and not value.strip():
                raise ValueError(f"{name}.{field.name} cannot be empty")
        if unique and row["id"] in ids:
            raise ValueError(f"duplicate {name} id")
        ids.add(row["id"])
        if name != "provenance":
            if not row["source_refs"]:
                raise ValueError("empty source_refs")
            for ref in row["source_refs"]:
                if (
                    not isinstance(ref, dict)
                    or not ref.get("source_id")
                    or not ref.get("location")
                ):
                    raise ValueError("invalid source reference")
        if name == "provenance" and (
            not isinstance(row["license_refs"], list)
            or any(not isinstance(x, str) or not x for x in row["license_refs"])
        ):
            raise ValueError("invalid license_refs")
        if name == "proofs" and row["verification_status"] not in {
            "unverified",
            "valid",
            "invalid",
            "inconclusive",
        }:
            raise ValueError("invalid verification status")


def validate(tables, unique=True):
    for name, rows in tables.items():
        validate_rows(name, rows, unique=unique)
    problems = {r["id"] for r in tables["problems"]}
    sources = {r["id"] for r in tables["provenance"]}
    if any(r["problem_id"] not in problems for r in tables["proofs"]):
        raise ValueError("dangling problem foreign key")
    if any(
        ref["source_id"] not in sources
        for name in ("problems", "proofs")
        for r in tables[name]
        for ref in r["source_refs"]
    ):
        raise ValueError("dangling provenance foreign key")


def write_tables(directory, tables, unique=True):
    validate(tables, unique=unique)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        rows = [dict(row) for row in rows]
        if name == "provenance":
            for row in rows:
                if isinstance(row["acquired_at"], str):
                    row["acquired_at"] = datetime.fromisoformat(row["acquired_at"])
        table = pa.Table.from_pylist(rows, schema=SCHEMAS[name])
        dest = directory / f"{name}.parquet"
        tmp = dest.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="zstd", row_group_size=4096)
        os.replace(tmp, dest)


def read_tables(directory):
    return {
        name: pq.read_table(Path(directory) / f"{name}.parquet", schema=s).to_pylist()
        for name, s in SCHEMAS.items()
    }
