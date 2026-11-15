"""Populate the existing extraction cache independently for distinct source rows."""

from bisect import bisect_left
from functools import lru_cache, partial
import multiprocessing
import os
from pathlib import Path

import pyarrow.parquet as pq

from .acquire import selected_snapshots
from .adapters import PARALLEL_RECORD_SOURCES, record_adapter
from .adapters.records import Unsupported
from .common import read_json
from .environment import environment_for
from .extraction_cache import ExtractionCache, INPUT_CHECK_LIMITS
from .lean import LeanFailure
from .normalize import runtime_for


@lru_cache(maxsize=1)
def _json_array(path):
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"parallel extraction requires a JSON array in {path.name}")
    return data


def _group_rows(path, group, offset, size):
    if group is None:
        yield from _json_array(path)[offset : offset + size]
        return
    for batch in pq.ParquetFile(path).iter_batches(batch_size=512, row_groups=[group]):
        yield from batch.to_pylist()


def _extract_group(job, *, cache_dir, source, source_id, adapter, environment, runtime):
    path, entry, group, offset, size, selected = job
    cache = ExtractionCache(cache_dir)
    deferred = 0
    for index, row in enumerate(_group_rows(path, group, offset, size), offset):
        if selected is not None and index not in selected:
            continue
        try:
            cache.extract(
                source_id,
                entry,
                index,
                source,
                environment,
                lambda: adapter(row, environment, runtime),
            )
        except Unsupported:
            # normalize reads the cached outcome and records its source location.
            pass
        except LeanFailure as error:
            if error.reason not in INPUT_CHECK_LIMITS:
                raise RuntimeError(f"{entry['path']}#row={index}: {error}") from error
            # Retry under serial normalization once competing workers exit.
            deferred += 1
        except Exception as error:
            raise RuntimeError(f"{entry['path']}#row={index}: {error}") from error
    return cache.hits, cache.misses, deferred


def _group_sizes(path):
    if path.suffix == ".json":
        count = len(_json_array(path))
        for offset in range(0, count, 1000):
            yield None, min(1000, count - offset)
    else:
        metadata = pq.ParquetFile(path).metadata
        for group in range(metadata.num_row_groups):
            yield group, metadata.row_group(group).num_rows


def _row_groups(path, entry, indices):
    offset = 0
    for group, size in _group_sizes(path):
        end = offset + size
        selected = None
        if indices is not None:
            start_at = bisect_left(indices, offset)
            end_at = bisect_left(indices, end, lo=start_at)
            selected = frozenset(indices[start_at:end_at])
        if selected is None or selected:
            yield path, entry, group, offset, size, selected
        offset = end


def warm_record_extraction(work, source, workers, selection=None):
    """Leave normalization, deduplication, and report generation to the canonical stages."""
    allocation = int(
        os.environ.get("SLURM_CPUS_PER_TASK", len(os.sched_getaffinity(0)))
    )
    if source not in PARALLEL_RECORD_SOURCES:
        raise ValueError("parallel extraction requires a supported record source")
    if not 1 < workers <= allocation:
        raise ValueError(
            "extraction workers must be between two and the allocated CPUs"
        )
    work = Path(work)
    [(snapshot, manifest)] = selected_snapshots(work, [source])
    source_id = snapshot.name
    environment_id = environment_for(work, source_id, manifest)
    environment = read_json(work / "environments" / f"{environment_id}.json")
    if not environment.get("resolved"):
        raise ValueError("parallel extraction requires a resolved environment")
    runtime = runtime_for(work, environment_id)
    adapter = record_adapter(
        source, manifest.get("revision_label") or manifest["revision"]
    )
    chosen = read_json(selection)["snapshots"][source_id] if selection else None
    groups = []
    for entry in manifest["files"]:
        name = entry["path"]
        if chosen is not None and name not in chosen:
            continue
        path = snapshot / "original" / name
        if path.suffix not in {".json", ".parquet"} or path.name in {
            "lake-manifest.json",
            "manifest.json",
        }:
            continue
        indices = None
        if chosen is not None:
            if any(type(index) is not int for index in chosen[name]):
                raise ValueError("parallel extraction requires numeric row selections")
            indices = sorted(chosen[name])
        groups.extend(_row_groups(path, entry, indices))
    _json_array.cache_clear()
    if not groups:
        raise ValueError("no record groups selected for extraction")
    extract = partial(
        _extract_group,
        cache_dir=work / "extraction-cache",
        source=source,
        source_id=source_id,
        adapter=adapter,
        environment=environment,
        runtime=runtime,
    )
    completed = hits = misses = deferred = 0
    with multiprocessing.get_context("spawn").Pool(min(workers, len(groups))) as pool:
        for old, new, limited in pool.imap_unordered(extract, groups):
            hits += old
            misses += new
            deferred += limited
            visited = hits + misses
            if visited // 20000 > completed // 20000:
                print(f"Prepared extraction cache for {visited:,} rows", flush=True)
            completed = visited
        pool.close()
        pool.join()
    return {"cache_hits": hits, "cache_misses": misses, "deferred_rows": deferred}
