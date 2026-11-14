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
count. Adjust resource requests using `sbatch` options based on measured pilots;
no partition is selected by the template. Defaults are one CPU, 12 GiB memory,
40 GiB job-local storage, 24 hours, and a pre-limit signal three minutes early.
Lean has a separate 8 GiB address-space cap and finite inspection deadlines.

Every 15 minutes and at exit, a quota check reserves archive space and file
headroom below 80% of the soft limits. A checkpoint includes the acquired raw
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
