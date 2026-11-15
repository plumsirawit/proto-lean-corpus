"""One explicit adapter per active source; unsupported representations are reported."""

from functools import partial

from .records import ADAPTERS

PARALLEL_RECORD_SOURCES = {"goedel_pset_v1", "lean_workbook"}

__all__ = ["ADAPTERS", "PARALLEL_RECORD_SOURCES", "record_adapter"]


def record_adapter(source, revision):
    adapter = ADAPTERS[source]
    if source == "lean_workbook":
        return partial(adapter, upstream_revision=revision)
    return adapter
