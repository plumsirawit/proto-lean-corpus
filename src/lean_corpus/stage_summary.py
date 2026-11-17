"""Small integrity-bound extraction receipts for bounded batch orchestration."""

from pathlib import Path

from .common import file_hash, read_json, write_json


def write_extraction_summary(work, input_occurrences, problems, proofs):
    work = Path(work)
    write_json(work / 'reports/extraction-summary.json', {
        'schema_version': '1',
        'input_occurrences': input_occurrences,
        'problem_occurrences': problems,
        'proof_occurrences': proofs,
        'extraction_sha256': file_hash(work / 'reports/extraction.json'),
        'normalized_state': read_json(work / 'normalized/state.json'),
    })


def extraction_summary(work):
    """Refuse missing/stale receipts without loading the complete report."""
    work = Path(work)
    value = read_json(work / 'reports/extraction-summary.json')
    if (
        value.get('schema_version') != '1'
        or any(type(value.get(key)) is not int or value[key] < 0 for key in
               ('input_occurrences', 'problem_occurrences', 'proof_occurrences'))
        or value.get('extraction_sha256') != file_hash(work / 'reports/extraction.json')
        or value.get('normalized_state') != read_json(work / 'normalized/state.json')
        or (work / 'normalization-incomplete.json').exists()
    ):
        raise ValueError('missing, changed or incomplete extraction summary')
    return value
