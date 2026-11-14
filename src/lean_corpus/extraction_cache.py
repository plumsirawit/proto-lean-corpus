"""Resumable deterministic record extraction; operational failures are never cached."""

from pathlib import Path
from .common import digest, file_hash, read_json, write_json
from .lean import LeanFailure
from .adapters.records import Unsupported


def implementation_files():
    root = Path(__file__).parent
    return sorted(root.rglob("*.py")) + sorted(root.rglob("*.lean"))


def implementation_digest():
    root = Path(__file__).parent
    return digest(
        {str(p.relative_to(root)): file_hash(p) for p in implementation_files()}
    )


def code_stat():
    return [
        (str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in implementation_files()
    ]


class CacheCorruption(RuntimeError):
    pass


def save(path, record):
    write_json(path, {**record, "checksum": digest(record)})


class ExtractionCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.implementation = implementation_digest()
        self.code_stat = code_stat()
        self.hits = self.misses = 0

    def extract(self, source_id, entry, index, source_key, environment, extract):
        if code_stat() != self.code_stat:
            raise RuntimeError(
                "implementation changed during normalization; rerun with frozen code"
            )
        key = digest(
            {
                "version": "1",
                "source_id": source_id,
                "file_sha256": entry["sha256"],
                "row": index,
                "source_key": source_key,
                "environment": environment,
                "implementation": self.implementation,
            }
        )
        path = self.directory / f"{key}.json"
        enabled = environment.get("resolved", False)
        if enabled and path.exists():
            try:
                cached = read_json(path)
                checksum = cached.pop("checksum", None)
                if (
                    checksum != digest(cached)
                    or cached.get("key") != key
                    or cached.get("outcome") not in {"unsupported", "extracted"}
                ):
                    raise CacheCorruption("extraction cache integrity failure")
            except (OSError, ValueError, KeyError) as error:
                raise CacheCorruption(
                    "extraction cache unreadable or corrupt"
                ) from error
            self.hits += 1
            if cached["outcome"] == "unsupported":
                raise Unsupported(cached["reason"])
            return cached["result"]
        self.misses += 1
        try:
            result = extract()
        except LeanFailure as error:
            if error.reason != "unsupported_syntax":
                raise
            if enabled:
                save(path, {"key": key, "outcome": "unsupported", "reason": str(error)})
            raise Unsupported(str(error)) from error
        except Unsupported as error:
            if enabled:
                save(path, {"key": key, "outcome": "unsupported", "reason": str(error)})
            raise
        if enabled and result.get("_parser_imports_resolved", False):
            save(path, {"key": key, "outcome": "extracted", "result": result})
        return result
