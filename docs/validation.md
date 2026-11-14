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

## Numina pilot follow-up (2026-11-14)

The NuminaMath-LEAN snapshot at revision
`51fa67f1f647ae1ecd81eef9f19306aa8a7b3a94` was acquired locally (104,155 rows).
Its checking project is Mathlib revision
`9837ca9d65d9de6fad1ef4381750ca688774e608`, using Lean 4.15.0 and the project's
pinned dependencies. This extends version coverage beyond the original synthetic
Lean 4.19 tests; it does not establish compatibility with every historical version.

The first ten rows completed all five stages locally: eight retained statements
elaborated and six extracted proofs verified. Two proof responses exposed a
parser-shape mismatch caused by doc comments; two tasks had admitted supporting
lemmas. These were extraction coverage limits, not evidence that the upstream
mathematics was incorrect. Runtime failures in earlier attempts exposed a separate
bug: normalization could silently skip records. The follow-up implementation stops
on operational failures and blocks downstream stages until normalization completes.

A bounded worker was compared against the original isolated process checker on all
six original pilot proofs: statuses, target types, and axiom dependencies matched.
That comparison took 56.59 seconds including import startup, versus 936.31 seconds
for the original pilot verification stage. These timings measure different runs on
a shared host; they are pilot observations, not corpus throughput guarantees.
Actual dataset text, processed artifacts, checking environments, and detailed run
logs remain in ignored local work directories. No dataset publication is performed.

All 26 tests passed in the expanded regression suite, with real Lean required and
no skips. Additional coverage checks extraction failure/recovery, cache corruption,
original-row selections, documentation metadata, worker context/option isolation,
import-header restarts, bounded worker lifetime, wall timeouts, and read-only
fallback execution. A focused rerun checks source-defined metaprogramming fallback
and normalization after final validation changes. Corpus-scale results remain
separate from these synthetic regression results.
The final focused rerun passed all five checks in 112.40 seconds.
