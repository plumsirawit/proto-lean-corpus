import hashlib
import json
import os
from pathlib import Path

VERSION = "1"


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text(value):
    return value.replace("\r\n", "\n").replace("\r", "\n")


def identity(kind, value):
    return f"{kind}-v{VERSION}-{digest(value)}"


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(canonical(value) + "\n")
    os.replace(temp, path)


def refs(values):
    return sorted({canonical(x): x for x in values}.values(), key=canonical)


def safe_relative(value):
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return p
