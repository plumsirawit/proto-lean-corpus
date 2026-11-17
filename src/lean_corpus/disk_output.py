"""Job-local, replayable normalization output with bounded row buffers.

The caller owns retention and quota reservations. Spools are exclusive, retained
on failure, and never implicitly reused. Output uses the existing canonical JSON
and row policies. A stage marker must guard multi-file publication.
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .common import canonical
from .disk_schema import validate_occurrences, validate_parquet
from .schema import SCHEMAS


class RowSpool:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open("x", encoding="utf-8"):
            pass
        self.count = 0

    def append(self, row):
        payload = canonical(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload + "\n")
        self.count += 1

    def extend(self, rows):
        for row in rows:
            self.append(row)

    def __len__(self):
        return self.count

    def __iter__(self):
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def fragments(value):
    """Yield exactly canonical(value), admitting RowSpool in place of lists."""
    if isinstance(value, dict):
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield canonical(key) + ":"
            yield from fragments(value[key])
        yield "}"
    elif isinstance(value, (RowSpool, list)):
        yield "["
        for index, row in enumerate(value):
            if index:
                yield ","
            yield from fragments(row)
        yield "]"
    else:
        yield canonical(value)


def spool_digest(value):
    result = hashlib.sha256()
    for part in fragments(value):
        result.update(part.encode("utf-8"))
    return result.hexdigest()


def write_document(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        for part in fragments(value):
            stream.write(part)
        stream.write("\n")
    os.replace(temp, path)


def write_spooled_tables(directory, tables, staging, batch_size=32):
    """Validate every staged row and key before replacing any public table.

    Memory scales with a finite batch (and the largest individual source row).
    The new staging directory and SQLite key database remain as diagnostics.
    """
    if batch_size < 1:
        raise ValueError("positive batch size required")
    if set(tables) != set(SCHEMAS):
        raise ValueError("all three tables required")
    staging = Path(staging)
    staging.mkdir()
    # Validate originals before Arrow can coerce values.
    validate_occurrences(
        ((name, row) for name in SCHEMAS for row in tables[name]),
        staging / "original-validation.sqlite", unique=False,
    )
    for name, schema in SCHEMAS.items():
        with pq.ParquetWriter(staging / f"{name}.parquet", schema,
                              compression="zstd") as writer:
            batch = []
            for original in tables[name]:
                row = dict(original)
                if name == "provenance" and isinstance(row["acquired_at"], str):
                    row["acquired_at"] = datetime.fromisoformat(row["acquired_at"])
                batch.append(row)
                if len(batch) == batch_size:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch = []
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    counts = validate_parquet(staging, staging / "validation.sqlite", unique=False,
                              batch_size=batch_size)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in SCHEMAS:
        os.replace(staging / f"{name}.parquet", directory / f"{name}.parquet")
    return counts
