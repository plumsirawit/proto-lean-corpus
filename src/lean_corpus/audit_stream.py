"""Independent report reconciliation with memory bounded by one JSON result.

The SQLite index belongs in job-local storage, outside the archived workspace.
No verification verdict is computed or changed here.
"""

import json
import sqlite3


class JSONStream:
    def __init__(self, stream, block=65536):
        self.stream, self.block = stream, block
        self.buffer, self.offset, self.eof = "", 0, False
        self.decoder = json.JSONDecoder()

    def fill(self):
        more = self.stream.read(self.block)
        self.buffer = self.buffer[self.offset:] + more
        self.offset = 0
        self.eof = not more

    def peek(self):
        while True:
            while self.offset < len(self.buffer):
                char = self.buffer[self.offset]
                if char not in " \t\r\n":
                    return char
                self.offset += 1
            if self.eof:
                return ""
            self.fill()

    def expect(self, char):
        if self.peek() != char:
            raise ValueError(f"Expected JSON delimiter {char!r}")
        self.offset += 1

    def value(self):
        self.peek()
        while True:
            try:
                item, end = self.decoder.raw_decode(self.buffer, self.offset)
                # A number may end at a read boundary and continue next block.
                if end == len(self.buffer) and not self.eof:
                    self.fill()
                    continue
                self.offset = end
                return item
            except json.JSONDecodeError as error:
                if self.eof:
                    raise ValueError("Truncated or invalid JSON report") from error
                self.fill()

    def entries(self, object_mode=False):
        self.expect("{" if object_mode else "[")
        end = "}" if object_mode else "]"
        if self.peek() != end:
            while True:
                key = self.value() if object_mode else None
                if object_mode:
                    if not isinstance(key, str):
                        raise ValueError("JSON object key must be a string")
                    self.expect(":")
                yield key
                if self.peek() == end:
                    break
                self.expect(",")
        self.expect(end)


def report_items(path, *, arrays=(), objects=(), required=(), block=65536):
    """Yield (field, member key, value), streaming designated collections."""
    with path.open(encoding="utf-8") as stream:
        parser = JSONStream(stream, block)
        fields = set()
        for field in parser.entries(object_mode=True):
            if field in fields:
                raise ValueError(f"Duplicate report field {field}")
            fields.add(field)
            if field in arrays or field in objects:
                for key in parser.entries(object_mode=field in objects):
                    yield field, key, parser.value()
            else:
                yield field, None, parser.value()
        if parser.peek():
            raise ValueError("Unexpected data after JSON report")
        if missing := set(required) - fields:
            raise ValueError(f"Missing report fields: {sorted(missing)}")


class VerificationIndex:
    def __init__(self, path, report):
        # Exclusive creation prevents stale/reused indices from hiding results.
        with path.open("xb"):
            pass
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA cache_size=-8192")
        self.db.execute("CREATE TABLE results (kind TEXT, id TEXT, status TEXT, reason TEXT, PRIMARY KEY(kind,id)) WITHOUT ROWID")
        for field, key, item in report_items(report, arrays={"attempts"}, objects={"problems"}, required={"attempts", "problems", "limit", "policy"}):
            if field in {"attempts", "problems"}:
                identifier = item["proof_id"] if field == "attempts" else key
                try:
                    self.db.execute("INSERT INTO results VALUES (?,?,?,?)", (field, identifier, item["status"], item["reason"]))
                except sqlite3.IntegrityError as error:
                    raise ValueError(f"Duplicate verification result for {identifier}") from error
            elif field == "limit" and item is not None:
                raise ValueError("Full audit requires unlimited verification")
        self.db.commit()

    def pop(self, kind, identifier):
        row = self.db.execute("SELECT status,reason FROM results WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        if row is None:
            raise ValueError(f"Missing verification result for {kind}:{identifier}")
        self.db.execute("DELETE FROM results WHERE kind=? AND id=?", (kind, identifier))
        return row

    def finish(self):
        if self.db.execute("SELECT 1 FROM results LIMIT 1").fetchone():
            raise ValueError("Verification results did not independently match canonical IDs")
        self.db.close()
