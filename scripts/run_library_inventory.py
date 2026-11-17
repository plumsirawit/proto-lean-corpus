"""Inventory original library declarations with the pinned Lean parser in Slurm.

This only selects inputs. Pilots still use adapters.records.library, retaining the
entire original prefix. Unsupported targets and files without targets participate
in selection; operational parser failures prevent a complete inventory.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import tarfile
import time

from euler_quota import check
from overnight_controller import atomic, now
from lean_corpus.acquire import included, registry, selected_snapshots
from lean_corpus.common import file_hash, text
from lean_corpus.extraction_cache import implementation_digest
from lean_corpus.lean import inspect
from lean_corpus.runtime import RuntimeGuard
from lean_corpus.worker import close_all


def selections(rows, snapshot_id):
    """Nested hash-ranked original locations, independent of proof success."""
    ranked = sorted(rows, key=lambda r: (r['rank'], r['path'], r['key']))
    if len({(r['path'], r['key']) for r in ranked}) != len(ranked):
        raise ValueError('duplicate declaration locations require explicit resolution')
    result = {}
    for size in (10, 100, 1000):
        files = defaultdict(list)
        for row in ranked[:size]:
            files[row['path']].append(row['key'])
        result[size] = {'schema_version': '1', 'snapshots': {snapshot_id: dict(files)}}
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--source', choices=('mathlib4', 'tau_ceti'), required=True)
    p.add_argument('--stage', type=Path, required=True)
    p.add_argument('--control', type=Path, required=True)
    p.add_argument('--deadline-seconds', type=int, default=19800)
    args = p.parse_args()
    assert os.environ.get('SLURM_JOB_ID'), 'compute allocation required'
    workspace = args.workspace.resolve()
    work = workspace / 'corpus'
    [(snapshot, manifest)] = selected_snapshots(work, [args.source])
    guard = RuntimeGuard(work)
    [runtime] = (work / 'runtimes').glob('*.json')
    environment, mapping = guard.get(runtime.stem)
    config = registry('configs/sources.yaml')[args.source]
    out = workspace / 'library-inventory'
    out.mkdir(exist_ok=True)
    identity = {'snapshot_id': snapshot.name, 'implementation': implementation_digest(),
                'inventory_implementation': file_hash(Path(__file__)),
                'environment_id': runtime.stem, 'memory_mb': mapping.get('memory_mb'),
                'parse_timeout': mapping.get('parse_timeout'), 'runner': mapping.get('runner')}
    marker = out / 'identity.json'
    if marker.exists():
        assert json.loads(marker.read_text()) == identity, 'changed inventory implementation or limits'
    else:
        atomic(marker, identity)
    journal = out / 'files.jsonl'
    completed, records, failures = set(), [], []
    if journal.exists():
        for line in journal.read_text().splitlines():
            row = json.loads(line)
            if row['status'] == 'parsed':
                completed.add(row['path']); records.extend(row['records'])
            else:
                failures.append(row)
    # Failed files are retried; previous diagnostic lines always remain in journal.
    entries = [e for e in manifest['files'] if e['path'].endswith('.lean')
               and Path(e['path']).name != 'lakefile.lean'
               and included(e['path'], config.get('include'))]
    start = time.monotonic(); last = start; generation = 0; saved = []
    status = {'complete': False, 'source': args.source, 'job_id': os.environ['SLURM_JOB_ID'],
              'code_revision': os.environ['LC_CODE_REVISION'], **identity}

    def checkpoint():
        nonlocal generation, saved, last
        close_all()
        atomic(out / 'progress.json', {**status, 'at': now(), 'files_total': len(entries),
               'files_parsed': len(completed), 'occurrences': len(records), 'diagnostic_attempts': len(failures)})
        size = sum(f.stat().st_size + 2048 for f in workspace.rglob('*') if f.is_file())
        check(args.stage, size + 16 * 1024**2, 8)
        check(args.control, 4 * 1024**2, 8)
        generation += 1
        archive = args.stage / f'library-inventory-{args.source}-{status["job_id"]}-{generation}.tar'
        assert not archive.exists()
        partial = archive.with_suffix('.tar.partial')
        with tarfile.open(partial, 'w') as target:
            target.add(workspace, arcname=args.source)
        partial.replace(archive)
        sha = file_hash(archive)
        Path(str(archive) + '.sha256').write_text(f'{sha}  {archive}\n')
        assert file_hash(archive) == sha
        receipt = {**status, 'at': now(), 'archive': str(archive), 'sha256': sha,
                   'files_total': len(entries), 'files_parsed': len(completed), 'occurrences': len(records),
                   'diagnostic_attempts': len(failures), 'resume_only': not status['complete']}
        atomic(Path(str(archive) + '.json'), receipt)
        atomic(args.control / f'library-inventory-{args.source}-{status["job_id"]}.json', receipt)
        saved.append(archive)
        while len(saved) > 2:
            old = saved.pop(0)
            # Only this job's generated generations, after verified replacement.
            for suffix in ('', '.sha256', '.json'):
                Path(str(old) + suffix).unlink()
        last = time.monotonic()
        print(json.dumps(receipt), flush=True)

    try:
        with journal.open('a') as stream:
            for entry in entries:
                path = entry['path']
                if path in completed:
                    continue
                if time.monotonic() - start >= args.deadline_seconds:
                    raise TimeoutError('finite inventory deadline; resume checkpoint')
                original = snapshot / 'original' / path
                assert file_hash(original) == entry['sha256']
                try:
                    parsed = inspect(text(original.read_text()), environment, mapping)
                    targets = parsed['targets'] or [{'name': None}]
                    batch = []
                    for target in targets:
                        key = f'declaration={target["name"]}'
                        rank = hashlib.sha256(f'{snapshot.name}\0{path}\0{key}'.encode()).hexdigest()
                        batch.append({'path': path, 'key': key, 'rank': rank, 'target': target})
                    row = {'path': path, 'sha256': entry['sha256'], 'status': 'parsed',
                           'records': batch, 'errors': parsed['errors'], 'imports_resolved': parsed['imports_resolved']}
                    records.extend(batch); completed.add(path)
                except Exception as error:
                    row = {'path': path, 'sha256': entry['sha256'], 'status': 'failed',
                           'error': repr(error), 'at': now()}
                    failures.append(row)
                stream.write(json.dumps(row) + '\n'); stream.flush()
                if len(completed) % 100 == 0:
                    print(json.dumps({'files_parsed': len(completed), 'occurrences': len(records), 'path': path}), flush=True)
                if time.monotonic() - last >= 900:
                    checkpoint()
        if len(completed) != len(entries):
            raise RuntimeError('inventory retains unresolved parser failures; cannot select by dropping files')
        for size, selection in selections(records, snapshot.name).items():
            atomic(workspace / f'selection-{size}.json', selection)
        atomic(out / 'selection-summary.json', {'source_occurrences': len(records),
               'sizes': {str(n): {'selected': min(n, len(records)), 'source_exhausted_before_cap': len(records) < n}
                         for n in (10, 100, 1000)}, 'method': 'nested SHA256 rank of exact original locations'})
        guard.finish()
        status['complete'] = True
    except BaseException as error:
        status['error'] = repr(error)
        raise
    finally:
        checkpoint()


if __name__ == '__main__':
    main()
