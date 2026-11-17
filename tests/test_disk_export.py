"""Streamed exports retain the ordinary cache policy, views and failure gates."""

import importlib

import pytest

from lean_corpus.check_cache import store_check
from lean_corpus.common import digest, identity, read_json, write_json
from lean_corpus.export import export
from lean_corpus.schema import read_tables, write_tables
from lean_corpus.verify import cache_key
from test_schema_dedup import problem, proof, tables

module = importlib.import_module('lean_corpus.export')


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    env = {'resolved': True, 'fixture': 'export'}
    eid = identity('environment', env)
    p = problem(env=eid)
    attempts = [proof(p), proof(p, 'by sorry'), proof(p, 'by exact ?_')]
    data = tables([p], attempts)
    manifest = {'license_paths': ['LICENSE'], 'release_review': ['inspect upstream']}
    for provenance in data['provenance']:
        provenance['manifest_hash'] = digest(manifest)
        path = work / 'snapshots' / provenance['id']
        write_json(path / 'manifest.json', manifest)
        write_json(path / 'receipt.json', {'fixture': True})
        (path / 'original').mkdir()
        (path / 'original/LICENSE').write_text('fixture license\n')
    (work / 'locks').mkdir()
    write_json(work / 'locks/sources.json', {'fixture': True})
    write_json(work / 'environments' / f'{eid}.json', env)
    for attempt, status in zip(attempts, ['valid', 'invalid', 'inconclusive']):
        attempt['verification_status'] = status
        if status != 'inconclusive':
            key = cache_key(p, attempt, env)
            store_check(work / 'verification-cache' / f'{key}.json', key,
                        {'status': status, 'reason': 'fixture'})
    write_tables(work / 'canonical', data)
    for stage in ['normalized', 'canonical']:
        write_json(work / stage / 'state.json', {'fingerprint': 'fixture'})

    class Guard:
        def __init__(self, work):
            pass

        def get(self, environment_id):
            assert environment_id == eid
            return env, {'runner': 'process'}

        def finish(self):
            pass

    monkeypatch.setattr(module, 'RuntimeGuard', Guard)
    monkeypatch.setattr(module, 'check_snapshot', lambda path: read_json(path / 'manifest.json'))
    return work, data, Guard


@pytest.mark.parametrize('cache', ['intact', 'missing', 'corrupt', 'wrong-status'])
def test_spooled_export_matches_tables_views_and_quality(prepared, tmp_path,
                                                        monkeypatch, cache):
    work, _, _ = prepared
    if cache != 'intact':
        path = next((work / 'verification-cache').glob('*.json'))
        if cache == 'missing':
            path.unlink()
        elif cache == 'corrupt':
            path.write_text('{}')
        else:
            record = read_json(path)
            status = 'invalid' if record['result']['status'] == 'valid' else 'valid'
            store_check(path, record['key'], {'status': status, 'reason': 'fixture'})
    expected = export(work, tmp_path / 'ordinary')

    def forbid(*args):
        pytest.fail('bounded export loaded whole tables')

    monkeypatch.setattr(module, 'read_tables', forbid)
    result = export(work, tmp_path / 'bounded', spool_dir=tmp_path / 'spool')
    assert read_json(tmp_path / 'bounded/quality.json') == expected
    assert result == {'counts': expected['counts'],
                      'verification_status': expected['verification_status'],
                      'quality_report': str(tmp_path / 'bounded/quality.json')}
    for view in ['', 'views/proof-discovery', 'views/verified']:
        assert read_tables(tmp_path / 'bounded' / view) == read_tables(tmp_path / 'ordinary' / view)
    for path in (tmp_path / 'ordinary').rglob('*'):
        if path.is_file() and path.suffix != '.parquet':
            other = tmp_path / 'bounded' / path.relative_to(tmp_path / 'ordinary')
            if path.name == 'quality.json':
                assert read_json(path) == read_json(other)
            else:
                assert path.read_bytes() == other.read_bytes()


@pytest.mark.parametrize('failure', ['runtime', 'changed', 'manifest', 'schema'])
def test_export_failure_never_publishes_and_preserves_input(prepared, tmp_path,
                                                           monkeypatch, failure):
    work, data, guard = prepared
    if failure == 'runtime':
        def fail(self):
            raise ValueError('changed runtime')
        monkeypatch.setattr(guard, 'finish', fail)
    elif failure == 'changed':
        def change(self):
            data['proofs'][0]['informal_proof'] = 'changed concurrently'
            write_tables(work / 'canonical', data)
        monkeypatch.setattr(guard, 'finish', change)
    elif failure == 'manifest':
        write_json(work / 'snapshots/s1/manifest.json', {'license_paths': [], 'release_review': []})
    else:
        # Deliberately bypass writer validation: malformed foreign keys cannot export.
        import pyarrow as pa
        import pyarrow.parquet as pq
        from lean_corpus.schema import SCHEMAS
        data['proofs'][0]['problem_id'] = 'absent'
        pq.write_table(pa.Table.from_pylist(data['proofs'], schema=SCHEMAS['proofs']),
                       work / 'canonical/proofs.parquet')
    with pytest.raises(ValueError):
        export(work, tmp_path / 'release', spool_dir=tmp_path / 'failed')
    assert not (tmp_path / 'release').exists()
    assert (tmp_path / 'failed').is_dir()
    assert read_tables(work / 'canonical') == data


def test_spools_exclusive_and_failed_export_can_recover(prepared, tmp_path):
    work, _, _ = prepared
    spool = tmp_path / 'spool'
    spool.mkdir()
    with pytest.raises(FileExistsError):
        export(work, tmp_path / 'release', spool_dir=spool)
    export(work, tmp_path / 'release', spool_dir=tmp_path / 'recovered')
    with pytest.raises(ValueError, match='destination already exists'):
        export(work, tmp_path / 'release', spool_dir=tmp_path / 'unused')
    assert not (tmp_path / 'unused').exists()
