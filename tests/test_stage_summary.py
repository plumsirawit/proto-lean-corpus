"""Batch receipts must fail closed and bounded orchestration must run end to end."""

import importlib.util
from pathlib import Path
import sys

import pytest

from lean_corpus.common import read_json, write_json
from lean_corpus.stage_summary import extraction_summary, write_extraction_summary


@pytest.mark.parametrize('damage', ['report', 'state', 'marker', 'count', 'missing'])
def test_summary_rejects_stale_or_incomplete_extraction(tmp_path, damage):
    work = tmp_path / 'work'
    write_json(work / 'reports/extraction.json', {'input_occurrences': 7, 'reports': []})
    write_json(work / 'normalized/state.json', {'fingerprint': 'fixture'})
    write_extraction_summary(work, 7, 5, 3)
    assert extraction_summary(work)['input_occurrences'] == 7
    if damage == 'report':
        write_json(work / 'reports/extraction.json', {'input_occurrences': 8})
    elif damage == 'state':
        write_json(work / 'normalized/state.json', {'fingerprint': 'changed'})
    elif damage == 'marker':
        write_json(work / 'normalization-incomplete.json', {'state': 'failed'})
    elif damage == 'count':
        path = work / 'reports/extraction-summary.json'
        write_json(path, {**read_json(path), 'input_occurrences': True})
    else:
        (work / 'reports/extraction-summary.json').unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        extraction_summary(work)


def driver():
    spec = importlib.util.spec_from_file_location('batch_driver', 'scripts/run_local_corpus.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('mode', ['inside', 'parallel'])
def test_batch_driver_rejects_unsafe_spool_configuration(tmp_path, monkeypatch, mode):
    work = tmp_path / 'workspace/corpus'
    work.mkdir(parents=True)
    root = tmp_path / 'spools' if mode == 'parallel' else work / 'spools'
    root.mkdir()
    monkeypatch.setattr(sys, 'argv', ['driver', '--work-dir', str(work),
        '--output-dir', str(tmp_path / 'export'), '--source', 'numina_math_lean',
        '--dataset-revision', 'fixture', '--expected-input-rows', '3',
        '--spool-root', str(root), '--workers', '2' if mode == 'parallel' else '1'])
    with pytest.raises(SystemExit) as error:
        driver().main()
    assert error.value.code == 2
    assert not list(root.iterdir())


@pytest.mark.lean
def test_real_bounded_batch_driver_preserves_all_attempts(real_lean, tmp_path, monkeypatch):
    from lean_corpus.acquire import acquire, check_snapshot
    from lean_corpus.schema import read_tables

    work = tmp_path / 'workspace/corpus'
    fixture = Path('tests/fixtures/numina').resolve()
    sid = acquire(work, Path('configs/sources.yaml'), 'numina_math_lean', fixture)
    manifest = check_snapshot(work / 'snapshots' / sid)
    write_json(work / 'runtimes' / (manifest['environment_id'] + '.json'),
               {'project_dir': str(fixture), 'lean_path': []})
    root = tmp_path / 'spools'
    root.mkdir()
    output = tmp_path / 'workspace/export'
    monkeypatch.setattr(sys, 'argv', ['driver', '--work-dir', str(work),
        '--output-dir', str(output), '--source', 'numina_math_lean',
        '--dataset-revision', manifest['revision'], '--expected-input-rows', '3',
        '--probe-import', 'Init', '--spool-root', str(root)])
    module = driver()
    ordinary_read = module.read_json

    def no_large_report(path):
        assert Path(path).name != 'extraction.json', 'batch driver loaded whole report'
        return ordinary_read(path)

    monkeypatch.setattr(module, 'read_json', no_large_report)
    module.main()
    state = read_json(work.parent / 'full-run-state.json')
    assert state['phase'] == 'complete' and state['bounded_stages'] is True
    assert extraction_summary(work)['input_occurrences'] == 3
    attempts = read_tables(output)['proofs']
    assert len(attempts) == 4
    assert sorted(p['verification_status'] for p in attempts) == [
        'invalid', 'invalid', 'valid', 'valid',
    ]
    [spools] = root.iterdir()
    assert {p.name for p in spools.iterdir()} == {'normalize', 'deduplicate', 'verify', 'export'}
    assert not any(p.name.startswith('corpus-stages-') for p in work.parent.rglob('*'))
