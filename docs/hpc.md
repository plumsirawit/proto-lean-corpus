# Local Euler batch runs

Keep the Git checkout in home. Place downloaded datasets, working outputs and
checkpoint archives in personal scratch; stage the many small cache files and
checking dependencies into `$TMPDIR` on a compute node. Source data never enters
Git and this template never acquires or publishes datasets. The acquisition stage
must already have produced an immutable snapshot with its pinned revision.

The [Euler storage guide](https://docs.hpc.ethz.ch/hardware/storage/) documents both
byte and file quotas, the lack of scratch backups and its 15-day cleanup policy.
Keep recovery archives of important raw inputs/results in appropriate durable
storage. Do not change timestamps to defeat cleanup. Use
[Slurm](https://docs.hpc.ethz.ch/batchsystem/slurm/) for sustained computation.

`scripts/euler_full.sbatch` restores one prepared checking project and toolchain,
clones an exact Git revision into job-local storage, rebinds the local runtime,
checks the actual Lean kernel/import environment, and runs normalization,
deduplication, verification and local export. It requires exactly one runtime
mapping, as in the Numina pilot. The default probe module is Mathlib.

Prepare three existing archives, without credentials:

- A plain tar of the built checking project, with project files at its root.
- A plain tar containing the installed `toolchains/<name>` directory's child
  `<name>` (the template extracts it into `$TMPDIR/elan/toolchains`).
- A plain tar of the acquired workspace folder, containing `corpus/` with
  snapshots, environment resolution and runtime mapping. Preserve original
  paths, receipts and manifests. Existing completed caches can be included.

Create SHA-256 manifests with absolute archive paths. The input archive needs
`<input-archive>.sha256`; `LC_CHECKSUMS` lists the project and toolchain archives.
All referenced paths must be available to compute nodes. Configure:

```sh
export LC_REPO="$HOME/proto-lean-corpus"
export LC_STAGE="$SCRATCH/proto-lean-corpus"
export LC_CODE_REVISION="$(git -C "$LC_REPO" rev-parse HEAD)"
export LC_INPUT_ARCHIVE=/path/to/workspace.tar
export LC_WORKSPACE_NAME=workspace
export LC_PROJECT_ARCHIVE=/path/to/built-project.tar
export LC_TOOLCHAIN_ARCHIVE=/path/to/toolchain.tar
export LC_CHECKSUMS=/path/to/checking-archives.sha256
export LC_DATASET_REVISION=PINNED_DATASET_COMMIT
export LC_EXPECTED_ROWS=EXPECTED_ORIGINAL_ROW_COUNT
sbatch --output="$LC_STAGE/full-%j.log" scripts/euler_full.sbatch
```

Start with a small smoke run by setting `LC_LIMIT=10` and `LC_EXPECTED_ROWS=10`.
Unset `LC_LIMIT` for the full acquired dataset and set its exact original row
count. The driver refuses a changed dataset revision or unexpected visited-row
count. For a deterministic selected pilot, set `LC_SELECTION_FILE` to a selection
JSON path relative to the archived workspace, unset `LC_LIMIT`, and set
`LC_EXPECTED_ROWS` to the number of selected original rows. The selection file
must remain inside that workspace. Adjust resource requests using `sbatch` options based on measured pilots;
no partition is selected by the template. Defaults are one CPU, 12 GiB memory,
40 GiB job-local storage, 24 hours, and a pre-limit signal three minutes early.
Lean has a separate 8 GiB address-space cap and finite inspection deadlines.

Every 15 minutes and at exit, a quota check reserves archive space and file
headroom below 80% of the soft limits. It queries the destination directory’s
filesystem directly; an unrelated home quota RPC failure cannot block a scratch
checkpoint. An unavailable destination quota check still prevents the write. A checkpoint includes the acquired raw
snapshot and completed caches. Temporary JSON writes are excluded. The archive,
SHA-256 file and metadata are written before atomically updating
`full-<job-id>-latest.json`. Only two identified generations for that job are
retained. Do not confuse a periodic checkpoint with a finished dataset: its
metadata says `resume_only`. Only a successful final archive whose run state is
`complete` is marked complete. `LC_CHECKPOINT_SECONDS` controls the interval.

To resume, read the latest metadata's `archive` path, use it as
`LC_INPUT_ARCHIVE`, and submit a new job with the same workspace name and pinned
checking/code versions. The stages rerun from raw inputs and reuse only completed,
integrity-checked inspections. Interrupted normalized/canonical tables are replaced;
operational failures are retried, never promoted to valid proofs. Each job's Git
snapshot stays fixed even if the home checkout changes.

Monitor with `squeue`, `sacct`, the job log and checkpoint metadata. A completed
processing run can still contain invalid, resource-limited or unsupported records;
inspect its quality and verification/extraction reports before any later release.
Dataset publication is a separate action and is not implemented by these scripts.

The export's `reports/effective-execution.json` records the actual inspection
limits, worker backend/lifetime, project-relative module paths and checking policy.
Use it in preference to example defaults in `configs/pipeline.yaml`. Machine-local
project paths stay out of the report. Original Lean task options remain unchanged.

Parallel verification is opt-in: `lean-corpus verify --work-dir WORK --workers 8`.
Each process runs the same serial checker in its own workspace and Lean session;
a problem and all its attempts belong to exactly one shard. Completed statement
and proof caches keep their existing identities and remain in the ordinary corpus
cache directories. Only the parent merges results, in original table order, after
all shards succeed and the input/runtime guards pass. An interrupted run resumes
from these caches; incomplete shard tables are never exported as final results.
Use one controller per corpus. Resource exhaustion can still produce inconclusive
results and is retried on resume.

For `euler_full.sbatch`, request the matching allocation (for example,
`--cpus-per-task=8 --mem-per-cpu=12G`) and set `LC_VERIFY_WORKERS=8`.
The runner uses a Slurm step bound to its allocated cores and rejects workers above
`SLURM_CPUS_PER_TASK`. Check current account/QoS limits and other jobs first;
Mathlib checking also needs substantial memory per process. The allocation, not
just the worker count, must fit the user's aggregate CPU and memory limits.
`LC_RESUME_VERIFICATION=1` explicitly reuses a checkpoint's completed full
normalization/deduplication without rerunning extraction under a new orchestration
version. It requires matching source/revision/row-count metadata and current
normalized/canonical stage state, and rejects changed pilot selections. Original
normalization provenance is retained in the resumed run state. Both modes keep
inputs, caches, checkpoints and exports local and perform no dataset publication.

For bounded multi-source pilots, `scripts/euler_pilot.sbatch` accepts only
`goedel_pset_v1`, `lean_workbook`, or `putnam_bench` through `LC_SOURCE`, and only
10, 100, or 1000 occurrences through `LC_PILOT_SIZE`. It uses the same `LC_REPO`,
`LC_STAGE`, `LC_CODE_REVISION`, and checksummed input/project/toolchain archive
settings. Toolchain archives contain a `toolchains/` directory; the input archive
contains a workspace named after the source key. Prepare all downloads and built
dependencies before submission: compute nodes may have no outbound network.

Record-source workspaces carry `selection-10.json`, `selection-100.json`, and
`selection-1000.json` manifests with nested selections of original rows. The
Putnam Lean-project pilot uses deterministic source order and a bounded limit; a
1000-occurrence pilot can exhaust a smaller snapshot, which is explicitly
recorded rather than duplicating tasks. Run each size for all sources and inspect
the results before submitting the next size. No full run follows automatically.

The pilot driver checks the real Mathlib/kernel environment, repeats normalization
byte-for-byte, verifies, exports locally, and records every non-valid proof,
invalid/inconclusive statement, and extraction diagnostic in `failures.json`.
Operational checking failures stop the pilot gate. Each job saves its workspace,
checksum, and completion metadata even when the driver fails. Use only metadata
with `complete=true` and `runner_exit=0` to advance. Each per-process checker uses
one Lean thread, matching the worker backend and avoiding host-wide thread pools
under a finite address-space limit.

`LC_CHECKER_MEMORY_MB` sets the per-checker virtual address-space cap (default
8192 MiB). This differs from the Slurm allocation's resident-memory limit:
memory-mapped Lean module files can require substantially more address space than
resident RAM. Measure the prepared environment before choosing both limits. The
pilot metadata records the actual cap, and parallel verification releases idle
parent checkers before starting its worker pool.

For targeted compatibility tests, `LEAN_CORPUS_TEST_TOOLCHAIN` overrides the core
test fixture's default Lean 4.19 toolchain. Supply an installed toolchain and run
the relevant core/adapter tests; this does not install tools or rewrite source
environment manifests.
