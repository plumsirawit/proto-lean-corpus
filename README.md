# proto-lean-corpus

A local, reproducible pipeline for assembling a Lean 4 **proof-discovery** corpus. The public data contract has exactly three typed Parquet tables: `problems`, `proofs`, and `provenance`. Multiple attempts, including invalid and incomplete proofs, stay associated with a fixed formal task. Original contributions to the processing code use the repository's Apache 2.0 license.

## Setup and fixture run

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/), Linux with unprivileged user/mount/PID/network namespaces, and an installed Lean toolchain for parsing. The tested helper toolchain is `leanprover/lean4:v4.19.0`; environments use their own recorded versions for checking. Processing never implicitly installs Lean, builds dependencies, or selects a latest Mathlib. The checker respects `ELAN_HOME`; installed toolchains and prepared dependency builds can be copied to local disk on hosts with slow shared filesystems, while raw and processed data remain in the chosen work directory.

```sh
uv sync --locked
elan toolchain install leanprover/lean4:v4.19.0
uv run --locked pytest -rs
make lint
make demo
```

`make demo` acquires only synthetic fixtures, exercises all five stages, and writes `.work/demo-export`. Use a new output directory for another export. Real kernel tests carry the `lean` marker; unavailable tooling/isolation is an explicit skip with a reason. No mocked result is counted as kernel verification.

## Commands

Run commands from the repository root. Workspaces and exports should be outside any input snapshot. All remote acquisition is explicit. These examples are **instructions for later runs**, not a claim that upstream payloads have been acquired or verified.

```sh
uv run lean-corpus sources

# Import a selected dataset directory, including its original docs/licenses.
uv run lean-corpus acquire --work-dir .work/corpus \
  --source numina_math_lean --local /path/to/NuminaMath-LEAN

# Or acquire a pinned upstream HF snapshot. Restrict downloads for a small run.
uv run lean-corpus acquire --work-dir .work/corpus \
  --source numina_math_lean --revision COMMIT_SHA --include 'data/SELECTED_SHARD.parquet'

uv run lean-corpus normalize --work-dir .work/corpus --source numina_math_lean --limit 20
uv run lean-corpus deduplicate --work-dir .work/corpus
uv run lean-corpus verify --work-dir .work/corpus --limit 10 --timeout 60
uv run lean-corpus export --work-dir .work/corpus --output-dir .work/release-001
```

`--source` on normalization is repeatable. `--limit` bounds input records (library declarations) globally, in deterministic snapshot/path order; verification bounds attempts and separately bounds statement checks. JSON container files are read as a unit, while JSONL and Parquet inputs stream in rows/batches. Deduplication currently holds the selected canonical corpus in memory; choose bounded shards and sufficient RAM for later large runs.

The eight active sources and the explicitly deferred AceReason entry are in [configs/sources.yaml](configs/sources.yaml). Git routes support Mathlib, Tau Ceti, and the official Putnam Lean project. HF routes use `huggingface_hub`, resolve the chosen ref to a commit, and record file hashes. Authentication uses the standard HF credential mechanism; credentials are not written into manifests. The existing staging bucket can be imported by first downloading a selected prefix to a local directory using the user's bucket tooling, then using `--local`. The pipeline does not modify the bucket. A bucket/local revision is a content-snapshot digest; an optional `--revision` label does not assert upstream byte identity.

Acquisition preserves original relative paths, docs, licenses, NOTICE files, and file-level notices. `.git`, `.lake`, and other generated directories are excluded. Symlinked inputs are rejected explicitly. Every reuse checks the complete inventory and SHA-256 hashes. Processing never edits originals. A network acquisition invoked again can resolve a new upstream ref; normalization always uses the already acquired immutable snapshots and locks, with no network requests.

## Checking environments

A toolchain hint in a dataset card does not resolve an environment. Dataset-only snapshots normally remain unresolved and verification returns `inconclusive`. Prepare the exact project/toolchain and pinned dependencies separately, without running `lake update` against a chosen latest Mathlib. Then explicitly associate it with a snapshot:

```sh
uv run lean-corpus resolve-environment --work-dir .work/corpus \
  --snapshot-id source-v1-SHA256 --project-dir /path/to/pinned-project \
  --lean-path .lake/build/lib/lean \
  --lean-path .lake/packages/mathlib/.lake/build/lib/lean \
  --lean-path .lake/packages/batteries/.lake/build/lib/lean
```

Pass a search path for **each** built dependency needed by the project, using the directory layout of that Lean/Lake version. The command preserves project inputs under `environment-projects/`, records the configuration and pinned dependency revisions, and creates a new environment identity. Rerun normalize and deduplicate afterward: resolving an environment changes problem IDs and therefore proof IDs. Originals and old environment manifests stay intact. Runtime paths are local operational mappings and are excluded from exports. Set a finite address-space limit with `lean-corpus runtime ... --memory-mb 8192` when the default 4096 MiB is insufficient for a full Mathlib import.

For a snapshot whose manifest already has a resolved environment, `lean-corpus runtime --work-dir ... --environment-id ... --project-dir ... --lean-path ...` registers its prepared checkout. Configuration, recorded source hashes, and clean dependency Git revisions are checked before verification. Compilation requires installed toolchains and built `.olean` dependencies; unavailable imports are inconclusive. Tau Ceti always uses its acquired project's dependency lock.

The helper uses Lean's command parser with the recorded imports, elaborating context commands so local notation and syntax extensions can be registered. During extraction only, theorem proof bodies are temporarily masked for elaboration, while the original parsed bodies are retained; this prevents failed or expensive attempts from disappearing during extraction. Verification executes the original supplied terms. Imported package initializers are enabled, as in Lean’s normal frontend. If imports are unavailable, extraction can fall back to the installed 4.19 core parser while preserving the original source; unknown syntax is reported as unsupported. This fallback never establishes proof validity. Other toolchains may need a version-specific helper update; helper compilation/import failures remain inconclusive and are visible in logs. Current acceptance covers the installed 4.19 integration environment, not all historical Mathlib versions.

## Output and guarantees

See [docs/contract.md](docs/contract.md) for schemas, fingerprint rules, merge precedence, stage artifacts, and verification policy, and [docs/adapters.md](docs/adapters.md) for source-specific supported forms and rejection behavior.

Exports contain central Parquet tables, a proof-discovery problem view, a view restricted to locally valid proofs, source manifests/locks, license and attribution artifacts, environment manifests, preserved resolved project inputs, build configuration, implementation revision/digest, and quality reports. Paths are relative to the release. Large original payloads stay in the acquisition workspace; their full path/hash inventory is included in the release. Nullable upstream text stays metadata. Remote publication is not implemented as an export side effect.

The proposed dataset policy is CC BY-SA 4.0 for original contributions and applicable compilation/database rights, with each upstream license preserved. Missing or uncertain attribution remains a release-review item. This policy does not replace upstream permissions. PutnamBench is eligible training content and is **not** held out. Deduplication makes no benchmark contamination guarantee; no split is assigned.

Stages replace their outputs on rerun rather than appending. Snapshots are immutable, exact duplicates merge by content identity, and downstream commands detect changed normalized inputs. Proof statuses are reused only through a cache keyed by task, proof, environment, policy and implementation; transient infrastructure failures are retried. Export resets stale terminal statuses to `unverified`. A pre-existing export directory is never overwritten. Interrupted multi-file stage writes must be rerun before proceeding; no database or distributed scheduling layer is provided.

No full corpus run, paid compute, bucket mutation, model inference, synthetic proof generation, or dataset publication is part of the fixture implementation. Correspondence between formal tasks and informal mathematics remains outside the automated checks.

Actual local test results and integration limits are recorded in [docs/validation.md](docs/validation.md).

## Long local runs and extraction recovery

Normalization stops on checker timeouts, unavailable imports in a resolved environment,
resource exhaustion, sandbox failures, or corrupt extraction caches. It leaves
`normalization-incomplete.json`; deduplication, verification, and export refuse that
workspace until normalization completes successfully. Unsupported input syntax is
reported separately. Completed extractions in resolved environments are cached by
snapshot, original file hash, row, environment, and processing implementation. A
rerun resumes completed records and replaces stage tables without appending rows.
Changing processing code invalidates extraction caches; changing code during a
normalization run aborts the stage.

The default checker starts a separate isolated Lean process for each inspection.
For prepared, pinned projects, an optional sequential worker reduces repeated import
costs:

```sh
uv run lean-corpus runtime --work-dir .work/corpus \
  --environment-id environment-v1-SHA256 --project-dir /path/to/pinned-project \
  --lean-path .lake/build/lib --memory-mb 8192 \
  --runner worker --worker-max-cases 128
```

Repeat `--lean-path` for required dependencies. Each worker remains inside the same
read-only filesystem and network isolation as the process checker, has finite
memory/CPU/output limits, and is replaced after the configured number of checks.
Each request starts with fresh command state; only an exact matching imported
environment is reused. Changed imports restart the worker. Explicit initialization
and metaprogramming commands fall back to separate processes. Per-request wall
limits terminate stalled workers; operational failures never count as valid proofs.
Workers assume the pinned imported Lean extensions are trusted and compatible with
reuse; use the default process backend when independent process state is required.
Backend changes invalidate verification caches.

For representative pilots, `normalize --selection /path/to/selection.json` selects
original row indices without rewriting the raw payload or renumbering provenance.
Use this instead of `--limit`:

```json
{"schema_version":"1","snapshots":{"source-v1-SHA256":{"data/train.parquet":[0,40,6897]}}}
```

Rows must be unique nonnegative integers, and every requested row must exist in a
selected acquired snapshot. Missing rows fail the stage. The exact selection is
recorded in the extraction report. For slow hosts, `runtime --parse-timeout 180`
sets the finite extraction deadline; verification has its separate `--timeout`.
