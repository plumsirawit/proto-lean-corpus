"""Optional job-local deduplication using the existing merge policy."""

from pathlib import Path

from .common import read_json, write_json
from .disk_merge import merge_groups, parquet_rows
from .disk_output import RowSpool, write_document, write_spooled_tables
from .disk_schema import validate_parquet
from .schema import SCHEMAS


def deduplicate_spooled(work, spool_dir):
    work, spool_dir = Path(work), Path(spool_dir)
    marker = work / "deduplication-incomplete.json"
    write_json(marker, {"state": "running"})
    try:
        spool_dir.mkdir()
        original_state = read_json(work / "normalized/state.json")
        validate_parquet(work / "normalized", spool_dir / "input.sqlite", unique=False)
        tables = {name: RowSpool(spool_dir / f"{name}.jsonl") for name in SCHEMAS}
        report = {name: RowSpool(spool_dir / f"{name}.jsonl")
                  for name in ("duplicate_groups", "metadata_conflicts")}
        report.update(before=dict.fromkeys(SCHEMAS, 0), after=dict.fromkeys(SCHEMAS, 0))
        for name, row, fragment in merge_groups(
            parquet_rows(work / "normalized"), spool_dir / "merge.sqlite"
        ):
            tables[name].append(row)
            for field in ("duplicate_groups", "metadata_conflicts"):
                report[field].extend(fragment[field])
            for field in ("before", "after"):
                report[field][name] += fragment[field][name]
        # Canonical keys must be unique even though normalized input keys need not be.
        from .disk_schema import validate_occurrences

        validate_occurrences(((name, row) for name in SCHEMAS for row in tables[name]),
                             spool_dir / "output.sqlite")
        if read_json(work / "normalized/state.json") != original_state:
            raise ValueError("normalized data changed during deduplication")
        write_spooled_tables(work / "canonical", tables, spool_dir / "tables")
        write_document(work / "reports/deduplication.json", report)
        write_json(work / "canonical/state.json", original_state)
    except BaseException as error:
        write_json(marker, {"state": "failed", "exception": type(error).__name__,
                            "reason": str(error)})
        raise
    marker.unlink()
    return report["after"]
