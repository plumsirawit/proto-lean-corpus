import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('inventory', Path(__file__).parents[1] / 'scripts/run_library_inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def test_nested_original_locations_preserve_unsupported_and_exhaustion():
    rows = [{'path': 'A.lean', 'key': f'declaration={i}', 'rank': f'{i:04}',
             'target': {'unsupported': 'unsupported syntax'} if i == 0 else {}} for i in range(23)]
    results = inventory.selections(rows, 'snapshot')
    assert results[10]['snapshots']['snapshot']['A.lean'] == [f'declaration={i}' for i in range(10)]
    assert results[100] == results[1000]
    assert len(results[100]['snapshots']['snapshot']['A.lean']) == 23
    assert inventory.selections(list(reversed(rows)), 'snapshot') == results
    with pytest.raises(ValueError, match='duplicate declaration locations'):
        inventory.selections(rows + [rows[0]], 'snapshot')
