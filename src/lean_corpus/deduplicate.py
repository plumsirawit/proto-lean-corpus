from collections import defaultdict
from pathlib import Path
from .common import canonical, read_json, refs, write_json
from .schema import read_tables, write_tables


def merge(tables):
    output, conflicts, groups = {}, [], []
    for name, rows in tables.items():
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["id"]].append(row)
        merged = []
        for row_id, values in sorted(grouped.items()):
            # Canonical reference serialization defines deterministic precedence.
            # Null metadata never outranks a present value.
            ordered = sorted(values, key=lambda r: canonical(r.get("source_refs", [])))
            row = dict(ordered[0])
            if name != "provenance":
                row["source_refs"] = refs(
                    [ref for v in values for ref in v["source_refs"]]
                )
                field = "informal_statement" if name == "problems" else "informal_proof"
                texts = [v[field] for v in ordered if v[field] is not None]
                row[field] = texts[0] if texts else None
                if len(set(texts)) > 1:
                    conflicts.append(
                        {
                            "table": name,
                            "id": row_id,
                            "field": field,
                            "values": sorted(set(texts)),
                        }
                    )
                if len(values) > 1:
                    groups.append(
                        {
                            "table": name,
                            "id": row_id,
                            "occurrences": len(values),
                            "source_refs": row["source_refs"],
                        }
                    )
            merged.append(row)
        output[name] = merged
    return output, {
        "duplicate_groups": groups,
        "metadata_conflicts": conflicts,
        "before": {k: len(v) for k, v in tables.items()},
        "after": {k: len(v) for k, v in output.items()},
    }


def require_normalized_complete(work):
    if (Path(work) / "normalization-incomplete.json").exists():
        raise ValueError(
            "normalization incomplete or failed: rerun normalize before downstream stages"
        )


def deduplicate(work):
    work = Path(work)
    require_normalized_complete(work)
    tables, report = merge(read_tables(work / "normalized"))
    write_tables(work / "canonical", tables)
    write_json(
        work / "canonical" / "state.json", read_json(work / "normalized" / "state.json")
    )
    write_json(work / "reports" / "deduplication.json", report)
    return report["after"]


def require_current(work):
    work = Path(work)
    require_normalized_complete(work)
    if read_json(work / "normalized" / "state.json") != read_json(
        work / "canonical" / "state.json"
    ):
        raise ValueError(
            "normalized data changed: run deduplicate before verify/export"
        )
