"""Cache integrity tests do not simulate or count successful kernel checks."""

import pytest
from lean_corpus.check_cache import CheckCacheCorruption, load_check, store_check
from lean_corpus.common import read_json, write_json


def test_completed_check_integrity_and_key(tmp_path):
    path = tmp_path / "entry.json"
    result = {"status": "elaborated", "reason": "synthetic cache payload"}
    store_check(path, "key", result)
    assert load_check(path, "key", {"elaborated"}) == result
    with pytest.raises(CheckCacheCorruption):
        load_check(path, "other-key", {"elaborated"})
    with pytest.raises(CheckCacheCorruption):
        load_check(path, "key", {"valid"})
    record = read_json(path)
    record["result"]["reason"] = "damaged"
    write_json(path, record)
    with pytest.raises(CheckCacheCorruption):
        load_check(path, "key", {"elaborated"})
