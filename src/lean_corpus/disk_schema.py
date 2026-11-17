"""Bounded validation for future full runs; stage drivers do not yet use this.

Row policies are shared with the existing validator. Cross-table keys and unique
IDs live in a new job-local SQLite database, retained on failure for diagnostics.
No source records or acceptance rules are rewritten by this component.
"""

from pathlib import Path
import sqlite3

from .disk_merge import parquet_rows
from .schema import SCHEMAS, validate_rows


def validate_occurrences(occurrences, database, unique=True):
    """Validate a single-pass (table, row) stream and return occurrence counts.

    References may precede their targets. With unique=False, duplicate IDs are
    permitted as in normalized inputs; every occurrence still gets row and key
    validation. The database is never silently overwritten or reused.
    """
    database = Path(database)
    with database.open("xb"):
        pass
    connection = sqlite3.connect(database)
    counts = dict.fromkeys(SCHEMAS, 0)
    try:
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(
            "CREATE TABLE ids (name TEXT, id TEXT, PRIMARY KEY(name,id)) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE refs (target TEXT, id TEXT, kind TEXT)"
        )
        with connection:
            for name, row in occurrences:
                if name not in SCHEMAS:
                    raise ValueError(f"unknown table {name}")
                validate_rows(name, [row], unique=False)
                result = connection.execute(
                    "INSERT OR IGNORE INTO ids VALUES (?, ?)", (name, row["id"])
                )
                if unique and result.rowcount != 1:
                    raise ValueError(f"duplicate {name} id")
                if name == "proofs":
                    connection.execute(
                        "INSERT INTO refs VALUES ('problems', ?, 'problem')",
                        (row["problem_id"],),
                    )
                if name != "provenance":
                    connection.executemany(
                        "INSERT INTO refs VALUES ('provenance', ?, 'provenance')",
                        ((ref["source_id"],) for ref in row["source_refs"]),
                    )
                counts[name] += 1
        # Match the existing validator's problem-before-provenance error order.
        for kind in ("problem", "provenance"):
            missing = connection.execute(
                "SELECT 1 FROM refs WHERE kind=? AND NOT EXISTS "
                "(SELECT 1 FROM ids WHERE ids.name=refs.target AND ids.id=refs.id) "
                "LIMIT 1", (kind,),
            ).fetchone()
            if missing:
                raise ValueError(f"dangling {kind} foreign key")
        return counts
    finally:
        connection.close()


def validate_parquet(directory, database, unique=True, batch_size=32):
    """Require exact schemas and validate all rows without loading whole tables."""
    return validate_occurrences(
        parquet_rows(directory, batch_size=batch_size), database, unique=unique
    )
