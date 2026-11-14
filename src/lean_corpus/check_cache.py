"""Integrity-checked completed inspections; these caches are not new kernel checks."""

from .common import digest, read_json, write_json


class CheckCacheCorruption(RuntimeError):
    pass


def store_check(path, key, result):
    payload = {"key": key, "result": result}
    write_json(path, {**payload, "checksum": digest(payload)})


def load_check(path, key, statuses):
    try:
        record = read_json(path)
        checksum = record.pop("checksum")
        if (
            checksum != digest(record)
            or record["key"] != key
            or record["result"]["status"] not in statuses
        ):
            raise ValueError("inspection checksum, key, or status mismatch")
        return record["result"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise CheckCacheCorruption(
            f"inspection cache integrity failure: {path.name}"
        ) from error
