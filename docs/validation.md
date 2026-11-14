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

At the original implementation handoff, the implementation was left uncommitted
for review. Subsequent source-only changes were committed with user authorization.
Exports record the code revision, dirty-worktree flag and actual implementation
digest. The user's implementation-plan Markdown is ignored by Git.

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

Runtime validation now hashes each pinned project once per verification/export
stage and rechecks its source, dependency revisions, environment manifest, and
runtime mapping before publishing stage output. This avoids rehashing the whole
Mathlib checkout for every proof. A synthetic mutation test verifies both bounded
validation calls and rejection of changed files/mappings/manifests. All 11 tests
without Lean passed. The real five-stage synthetic integration test passed in
29.76 seconds in Slurm job 20851223; namespace isolation also passed in job 20851051.

Further local pilots used deterministic category-balanced original-row selections
from the same pinned Numina snapshot. They test format coverage, not prevalence:
the 32-row pilot retained 32 problems / 40 attempts (21 valid, 16 admitted,
2 rejected by the axiom policy for Lean.ofReduceBool, 1 resource exhaustion).
The 100-row pilot retained 93 problems / 117 attempts (66 valid, 50 invalid,
1 resource exhaustion). Every retained statement elaborated, and repeated
normalization matched byte-for-byte. These observations preceded a further fix
for module-documentation-only response differences. The resource-limited attempt
reproduced the same classification in a fresh isolated process and a fresh worker;
it hit Lean's 200,000-heartbeat budget. Native reduction's extra axiom remains
excluded by the existing three-axiom policy; those labels do not assert false
mathematics.

The module-documentation and masked-proof fallback fixes passed seven focused
real Lean tests in Slurm job 20853805 (120.79 seconds). A subsequent real end-to-end
check passed in job 20854507 (34.04 seconds), including resuming completed statement
and proof checks without kernel re-execution and refusing corrupted cache evidence.
All 12 tests without Lean passed after the integrity-cache addition. Actual theorem
text and proof/data artifacts are confined to ignored local directories and scratch.

At source revision `63beb42`, strict GitHub CI passed all 29 tests (12 without Lean
and 17 with real Lean, no skips). A further category-balanced 1,000-row pilot in
Slurm job 20856149 completed processing: 931 retained statements all elaborated;
verification reported 632 valid, 507 invalid and 27 inconclusive resource-limited
attempts. Normalization produced 1,168 attempt occurrences before deduplication.
Repeated normalization was byte-identical. There were 86 extraction reports,
including unsupported supporting admissions and response contexts. These numbers
describe format coverage, not an estimate of the full dataset's quality.

The pilot finished its processing and wrote results before the job reached its
two-hour limit during final saving. Its archive exists and is readable; final
checksum/cleanup confirmation is a separate gate before reusing it. The pilot also
exposed empty target slots ending in bare `:=` and whitespace after `:= by`.
Support for those slots is undergoing a separate real-Lean regression and pilot;
the figures above precede that fix. Truncated binders and missing supporting
context remain unsupported rather than being repaired speculatively.

The quota-aware batch controller completed cold, completed-cache resume and
interrupted-checkpoint resume smoke runs (jobs 20860611, 20861141 and 20862289).
Each produced eight valid proofs from the first ten input rows. The completed-cache
resume executed only the synthetic runtime probe; corpus checks reused validated
cache evidence. Checkpoints and real data remain private on scratch. The controller
and its storage/resource settings are documented in [hpc.md](hpc.md).
