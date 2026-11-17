"""Bounded verification orchestration parity; checker acceptance stays unchanged."""

import importlib

import pytest

from lean_corpus.common import read_json, write_json
from lean_corpus.export import export
from lean_corpus.schema import read_tables, write_tables
from lean_corpus.verify import verify
from test_schema_dedup import problem, proof, tables

regular = importlib.import_module('lean_corpus.verify')
disk = importlib.import_module('lean_corpus.disk_verify')


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    a, b = problem(), problem(statement='theorem t : False := by sorry')
    c = problem(statement='theorem t : 1 = 1 := by sorry')
    data = tables([a, b, c], [proof(a), proof(a, 'by sorry'), proof(b)])
    write_tables(tmp_path / 'canonical', data)
    write_json(tmp_path / 'canonical/state.json', {'fingerprint': 'test'})
    write_json(tmp_path / 'normalized/state.json', {'fingerprint': 'test'})

    class Guard:
        def __init__(self, work):
            pass

        def get(self, env):
            return {'id': env, 'resolved': True}, {'runner': 'process'}

        def finish(self):
            pass

    def baseline(*args):
        return {'status': 'elaborated', 'reason': 'fixture'}

    def check(p, attempt, *args):
        return {'status': 'invalid' if attempt['formal_proof'] == 'by sorry' else 'valid',
                'reason': 'fixture'}

    for module in (regular, disk):
        monkeypatch.setattr(module, 'RuntimeGuard', Guard)
        monkeypatch.setattr(module, 'check_problem', baseline)
        monkeypatch.setattr(module, 'check_proof', check)
    return tmp_path, data, Guard


@pytest.mark.parametrize('limit', [None, 0, 1, 2, -1, 99])
def test_disk_verify_matches_regular_reports_and_tables(prepared, limit):
    work, data, _ = prepared
    # Warm both implementations' shared caches, then compare like-for-like reports.
    verify(work, limit=limit)
    expected = verify(work, limit=limit)
    report = read_json(work / 'reports/verification.json')
    rows = read_tables(work / 'canonical')
    write_tables(work / 'canonical', data)
    assert verify(work, limit=limit, spool_dir=work / 'spool') == expected
    assert read_json(work / 'reports/verification.json') == report
    assert read_tables(work / 'canonical') == rows
    assert not (work / 'verification-incomplete.json').exists()


def test_disk_verify_caches_completed_checks_only(prepared, monkeypatch):
    work, _, _ = prepared
    calls = []

    def check(p, attempt, *args):
        calls.append(attempt['id'])
        return {'status': 'inconclusive', 'reason': 'timeout'}

    monkeypatch.setattr(disk, 'check_proof', check)
    assert verify(work, spool_dir=work / 'one')['inconclusive'] == 3
    assert verify(work, spool_dir=work / 'two')['inconclusive'] == 3
    assert len(calls) == 6
    assert len(list((work / 'statement-cache').glob('*.json'))) == 3
    assert not (work / 'verification-cache').exists()


def test_runtime_mismatch_parity(prepared, monkeypatch):
    work, _, guard = prepared

    def fail(self, env):
        raise ValueError('changed project')

    monkeypatch.setattr(guard, 'get', fail)
    expected = verify(work)
    report = read_json(work / 'reports/verification.json')
    assert verify(work, spool_dir=work / 'spool') == expected
    assert read_json(work / 'reports/verification.json') == report


@pytest.mark.parametrize('failure', ['finish', 'report', 'changed'])
def test_failed_verification_blocks_export_and_recovers(prepared, monkeypatch, failure):
    work, _, guard = prepared
    before = read_tables(work / 'canonical')
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError('injected failure')
        if failure == 'finish':
            patch.setattr(guard, 'finish', fail)
        elif failure == 'report':
            patch.setattr(disk, 'write_document', fail)
        else:
            def change(*args):
                write_json(work / 'canonical/state.json', {'fingerprint': 'changed'})
            patch.setattr(guard, 'finish', change)
        with pytest.raises((RuntimeError, ValueError)):
            verify(work, spool_dir=work / 'failed')
    assert read_json(work / 'verification-incomplete.json')['state'] == 'failed'
    if failure == 'finish':
        assert read_tables(work / 'canonical') == before
        assert not list((work / 'verification-cache').glob('*.json'))
        assert not list((work / 'statement-cache').glob('*.json'))
    write_json(work / 'canonical/state.json', {'fingerprint': 'test'})
    with pytest.raises(ValueError, match='verification incomplete'):
        export(work, work / 'export')
    verify(work, spool_dir=work / 'recovery')
    assert not (work / 'verification-incomplete.json').exists()


def test_corrupt_cache_preserved_and_blocks_publication(prepared):
    work, _, _ = prepared
    verify(work, spool_dir=work / 'one')
    cache = next((work / 'verification-cache').glob('*.json'))
    cache.write_text('{}')
    with pytest.raises(RuntimeError, match='cache integrity'):
        verify(work, spool_dir=work / 'two')
    assert cache.read_text() == '{}'
    assert (work / 'verification-incomplete.json').exists()


def test_existing_spool_refused_and_regular_recovery(prepared):
    work, _, _ = prepared
    spool = work / 'spool'
    spool.mkdir()
    with pytest.raises(FileExistsError):
        verify(work, spool_dir=spool)
    assert (work / 'verification-incomplete.json').exists()
    verify(work)
    assert not (work / 'verification-incomplete.json').exists()


@pytest.mark.lean
def test_bounded_pipeline_uses_real_kernel_and_shared_caches(real_lean, tmp_path,
                                                           monkeypatch):
    from pathlib import Path
    from lean_corpus.acquire import acquire, check_snapshot
    from lean_corpus.deduplicate import deduplicate
    from lean_corpus.normalize import normalize

    work = tmp_path / 'work'
    registry = Path('configs/sources.yaml')
    fixture = Path('tests/fixtures/numina').resolve()
    sid = acquire(work, registry, 'numina_math_lean', fixture)
    env_id = check_snapshot(work / 'snapshots' / sid)['environment_id']
    write_json(work / 'runtimes' / f'{env_id}.json',
               {'project_dir': str(fixture), 'lean_path': []})
    assert normalize(work, registry, spool_dir=tmp_path / 'normalize') == {
        'problems': 3, 'proofs': 4, 'reports': 0,
    }
    deduplicate(work, spool_dir=tmp_path / 'deduplicate')
    expected = {'unverified': 0, 'valid': 2, 'invalid': 2, 'inconclusive': 0}
    assert verify(work, spool_dir=tmp_path / 'verify') == expected

    def unexpected(*args):
        pytest.fail('completed real inspection was not reused')

    for module in (regular, disk):
        monkeypatch.setattr(module, 'check_proof', unexpected)
        monkeypatch.setattr(module, 'check_problem', unexpected)
    assert verify(work) == expected
    report = read_json(work / 'reports/verification.json')
    assert verify(work, spool_dir=tmp_path / 'resume') == expected
    assert read_json(work / 'reports/verification.json') == report
    assert export(work, tmp_path / 'release')['verification_status'] == {
        status: count for status, count in expected.items() if count
    }
