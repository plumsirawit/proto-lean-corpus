"""Populate the existing extraction cache using independent Parquet row groups."""

from bisect import bisect_left
from functools import partial
import multiprocessing
import os
from pathlib import Path

import pyarrow.parquet as pq

from .adapters.records import Unsupported, goedel_pset_v1
from .acquire import selected_snapshots
from .common import read_json
from .environment import environment_for
from .extraction_cache import ExtractionCache
from .normalize import runtime_for


def _extract_group(job, *, cache_dir, source_id, environment, runtime):
    path, entry, group, offset, selected = job
    cache = ExtractionCache(cache_dir)
    index = offset
    for batch in pq.ParquetFile(path).iter_batches(batch_size=512, row_groups=[group]):
        for row in batch.to_pylist():
            if selected is None or index in selected:
                try:
                    cache.extract(
                        source_id,
                        entry,
                        index,
                        "goedel_pset_v1",
                        environment,
                        lambda: goedel_pset_v1(row, environment, runtime),
                    )
                except Unsupported:
                    # normalize reads the cached outcome and records its source location.
                    pass
                except Exception as error:
                    raise RuntimeError(
                        f"{entry['path']}#row={index}: {error}"
                    ) from error
            index += 1
    return cache.hits, cache.misses


def _row_groups(path, entry, indices):
    metadata = pq.ParquetFile(path).metadata
    offset = 0
    for group in range(metadata.num_row_groups):
        end = offset + metadata.row_group(group).num_rows
        selected = None
        if indices is not None:
            start_at = bisect_left(indices, offset)
            end_at = bisect_left(indices, end, lo=start_at)
            selected = frozenset(indices[start_at:end_at])
        if selected is None or selected:
            yield path, entry, group, offset, selected
        offset = end


def warm_goedel_extraction(work, workers, selection=None):
    """Leave normalization, deduplication, and report generation to the canonical stages."""
    allocation = int(
        os.environ.get("SLURM_CPUS_PER_TASK", len(os.sched_getaffinity(0)))
    )
    if not 1 < workers <= allocation:
        raise ValueError(
            "extraction workers must be between two and the allocated CPUs"
        )
    work = Path(work)
    [(snapshot, manifest)] = selected_snapshots(work, ["goedel_pset_v1"])
    source_id = snapshot.name
    environment_id = environment_for(work, source_id, manifest)
    environment = read_json(work / "environments" / f"{environment_id}.json")
    if not environment.get("resolved"):
        raise ValueError("parallel extraction requires a resolved environment")
    runtime = runtime_for(work, environment_id)
    chosen = read_json(selection)["snapshots"][source_id] if selection else None
    groups = []
    for entry in manifest["files"]:
        name = entry["path"]
        if not name.endswith(".parquet") or (chosen is not None and name not in chosen):
            continue
        indices = sorted(chosen[name]) if chosen is not None else None
        groups.extend(_row_groups(snapshot / "original" / name, entry, indices))
    if not groups:
        raise ValueError("no Parquet row groups selected for extraction")
    extract = partial(
        _extract_group,
        cache_dir=work / "extraction-cache",
        source_id=source_id,
        environment=environment,
        runtime=runtime,
    )
    completed = hits = misses = 0
    with multiprocessing.get_context("spawn").Pool(min(workers, len(groups))) as pool:
        for old, new in pool.imap_unordered(extract, groups):
            hits += old
            misses += new
            visited = hits + misses
            if visited // 20000 > completed // 20000:
                print(f"Prepared extraction cache for {visited:,} rows", flush=True)
            completed = visited
        pool.close()
        pool.join()
    return {"cache_hits": hits, "cache_misses": misses}
