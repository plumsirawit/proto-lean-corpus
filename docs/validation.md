# Implementation validation

Validated locally on 2026-11-13 with Python 3.10.12, PyArrow 20.0.0, and real Lean 4.19.0 (`6caaee842e94`).

All **18 distinct tests** passed across the suite and targeted reruns. The final end-to-end and internal-resource-budget checks passed together (`2 passed in 120.37s`). The end-to-end test was rerun after the verifier was finalized so implementation hashes stayed constant throughout verification and export. No mocked success is presented as kernel verification.

Coverage includes schemas/Parquet round trips, foreign keys, duplicate source/proof retention, changed environments/assumptions, metadata conflicts, immutable snapshots, explicit environment resolution, bounded HF notice preservation, all eight active adapters, empty and admitted attempts, multiple assistant turns, ambiguous response handling, incomplete Putnam definitions, library prefixes, local lemma syntax, named nested scopes, unauthorized transitive axioms, resource/infrastructure classification, relocatable exports, and repeat processing without extra logical rows.

The real Lean path checked valid, invalid, admitted, and unauthorized-axiom proofs. A real `maxRecDepth` exhaustion produced an inconclusive resource failure. A separate failure-injection test covers infrastructure reason classification only.

The retained final end-to-end fixture export is locally under:

```text
.work/final-tests/test_end_to_end0/relocated/
```

It contains 3 problems, 4 attempts, and 1 source snapshot. There are 2 valid and 2 invalid attempts, with none inconclusive or unverified. The verified view contains exactly the 2 valid attempts. The test moved the release and checked its relative license references. A separate documented demo run produced the same counts.

Reproduce the checks from a clean checkout:

```sh
uv sync --locked
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked pytest -q -rs
# Optional fixture export; select a fresh output directory.
uv run --locked python tests/demo.py --work-dir .work/demo-new --output-dir .work/export-new
```

The current implementation was left uncommitted for review. Exports honestly record the code revision, dirty-worktree flag and actual implementation digest. The user's implementation-plan Markdown is ignored by Git.

Full upstream acquisition, actual Mathlib/Tau Ceti dependency builds, per-snapshot version compatibility checks, corpus-scale memory/performance validation, and publication review remain the explicitly planned next phase. No full-corpus acquisition/run, paid compute, remote bucket mutation, or public data publication was performed. Source cards/previews and small parser definitions were inspected online; fixtures are synthetic.
