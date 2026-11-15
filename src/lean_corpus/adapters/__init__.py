"""One explicit adapter per active source; unsupported representations are reported."""

from functools import partial

from .records import ADAPTERS

__all__ = ["ADAPTERS", "record_adapter"]


def record_adapter(source, revision):
    adapter = ADAPTERS[source]
    if source == "lean_workbook":
        return partial(adapter, upstream_revision=revision)
    return adapter
