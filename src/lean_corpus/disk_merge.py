"""Disk-backed grouping for future full runs; not enabled by the stage drivers.

The existing merge function remains the authority for metadata precedence and
duplicate accounting. SQLite sorts occurrences on local disk, so memory scales
with one duplicate group, not the source. Oversized groups fail explicitly rather
than dropping references. Callers must still validate and atomically publish the
complete tables and reports before advancing a stage.
"""

from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq

from .common import canonical
from .deduplicate import merge
from .schema import SCHEMAS


def _parquet_schema(schema):
    # Parquet's compliant list encoding renames Arrow's `item` child to
    # `element`. Compare against the exact serialized schema, retaining nested
    # nullability, field names/types and metadata rather than ignoring metadata.
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_batches([], schema=schema), sink)
    return pq.ParquetFile(pa.BufferReader(sink.getvalue())).schema_arrow


def parquet_rows(directory, batch_size=32):
    """Read normalized occurrences in original table and row order."""
    if batch_size < 1:
        raise ValueError("positive batch size required")
    for name, schema in SCHEMAS.items():
        source = pq.ParquetFile(Path(directory) / f"{name}.parquet")
        actual = source.schema_arrow
        if not (
            actual.equals(schema, check_metadata=True)
            or actual.equals(_parquet_schema(schema), check_metadata=True)
        ):
            raise ValueError(f"unexpected {name} schema")
        for batch in source.iter_batches(batch_size=batch_size, use_threads=False):
            for row in batch.to_pylist():
                yield name, row


def merge_groups(occurrences, database, max_group_bytes=64 * 1024**2):
    """Yield (table, canonical row, group report) with bounded grouping memory.

    The database must be a new file on job-local storage. It is retained on any
    failure for diagnostics and never silently reused. Consume the iterator to
    completion before treating its output as a successful merge. Its report
    fragments can be streamed to disk rather than accumulated in memory.
    """
    if max_group_bytes < 1:
        raise ValueError("positive group byte limit required")
    database = Path(database)
    # Exclusive creation also prevents concurrent/retried runs from sharing data.
    with database.open("xb"):
        pass
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(
            "CREATE TABLE occurrences (table_number INTEGER, id TEXT, "
            "sequence INTEGER PRIMARY KEY, payload TEXT, bytes INTEGER)"
        )
        names = list(SCHEMAS)
        with connection:
            for sequence, (name, row) in enumerate(occurrences):
                if name not in SCHEMAS:
                    raise ValueError(f"unknown table {name}")
                if not isinstance(row.get("id"), str) or not row["id"].strip():
                    raise ValueError("nonempty row id required")
                row = dict(row)
                if name == "provenance" and isinstance(row["acquired_at"], datetime):
                    row["acquired_at"] = row["acquired_at"].isoformat()
                payload = canonical(row)
                connection.execute(
                    "INSERT INTO occurrences VALUES (?, ?, ?, ?, ?)",
                    (names.index(name), row["id"], sequence, payload,
                     len(payload.encode("utf-8"))),
                )
        connection.execute(
            "CREATE INDEX sorted_occurrences ON occurrences(table_number,id,sequence)"
        )
        groups = connection.execute(
            "SELECT table_number,id,SUM(bytes) FROM occurrences "
            "GROUP BY table_number,id ORDER BY table_number,id"
        )
        for number, row_id, group_bytes in groups:
            if group_bytes > max_group_bytes:
                raise ValueError(
                    f"duplicate group exceeds byte limit: {names[number]} {row_id} "
                    f"{group_bytes} > {max_group_bytes}; all occurrences retained"
                )
            values = []
            for (payload,) in connection.execute(
                "SELECT payload FROM occurrences WHERE table_number=? AND id=? "
                "ORDER BY sequence", (number, row_id),
            ):
                row = json.loads(payload)
                if names[number] == "provenance":
                    row["acquired_at"] = datetime.fromisoformat(row["acquired_at"])
                values.append(row)
            tables, report = merge({names[number]: values})
            yield names[number], tables[names[number]][0], report
    finally:
        connection.close()
