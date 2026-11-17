"""Stage parity and interrupted-publication guards for disk deduplication."""

import pytest

from lean_corpus.common import read_json, write_json
from lean_corpus.deduplicate import deduplicate, require_current
from lean_corpus.schema import read_tables, write_tables
from test_schema_dedup import problem, proof, tables


def prepare(work):
    a, b = problem(informal="A"), problem(source="s2", informal="B")
    write_tables(work / "normalized", tables([a, b], [proof(a), proof(b, source="s2")]),
                 unique=False)
    write_json(work / "normalized/state.json", {"fingerprint": "fixture"})


def test_spooled_deduplication_matches_existing_stage(tmp_path):
    prepare(tmp_path)
    expected = deduplicate(tmp_path)
    before = read_tables(tmp_path / "canonical")
    report = read_json(tmp_path / "reports/deduplication.json")
    assert deduplicate(tmp_path, spool_dir=tmp_path / "spool") == expected
    assert read_tables(tmp_path / "canonical") == before
    assert read_json(tmp_path / "reports/deduplication.json") == report
    require_current(tmp_path)


@pytest.mark.parametrize("failure", ["merge", "report", "changed"])
def test_failed_disk_deduplication_blocks_downstream_and_recovers(tmp_path, monkeypatch,
                                                                failure):
    import lean_corpus.disk_deduplicate as module

    prepare(tmp_path)
    deduplicate(tmp_path)
    before = read_tables(tmp_path / "canonical")
    with monkeypatch.context() as patch:
        if failure == "merge":
            def fail(*args):
                raise RuntimeError("injected merge failure")
            patch.setattr(module, "merge_groups", fail)
        elif failure == "report":
            def fail(*args):
                raise RuntimeError("injected report failure")
            patch.setattr(module, "write_document", fail)
        else:
            original = module.merge_groups

            def change(*args):
                yield from original(*args)
                write_json(tmp_path / "normalized/state.json", {"fingerprint": "changed"})
            patch.setattr(module, "merge_groups", change)
        with pytest.raises((RuntimeError, ValueError)):
            deduplicate(tmp_path, spool_dir=tmp_path / "failed")
    assert read_tables(tmp_path / "canonical") == before
    with pytest.raises(ValueError, match="deduplication incomplete"):
        require_current(tmp_path)
    assert read_json(tmp_path / "deduplication-incomplete.json")["state"] == "failed"
    deduplicate(tmp_path, spool_dir=tmp_path / "recovery")
    require_current(tmp_path)


def test_disk_deduplication_rejects_stale_spool(tmp_path):
    prepare(tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    with pytest.raises(FileExistsError):
        deduplicate(tmp_path, spool_dir=spool)
    with pytest.raises(ValueError, match="deduplication incomplete"):
        require_current(tmp_path)
    deduplicate(tmp_path)
    require_current(tmp_path)
