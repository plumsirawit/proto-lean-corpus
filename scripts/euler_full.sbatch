#!/bin/bash
#SBATCH --job-name=lean-corpus-local
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem-per-cpu=12G
#SBATCH --tmp=40G
#SBATCH --time=24:00:00
#SBATCH --signal=B:USR1@180
# Supply LC_* settings described in docs/hpc.md. This script never downloads or uploads datasets.
set -euo pipefail
umask 077
: "${SLURM_JOB_ID:?submit through sbatch}" "${TMPDIR:?job-local storage required}"
: "${LC_REPO:?}" "${LC_STAGE:?}" "${LC_CODE_REVISION:?}" "${LC_INPUT_ARCHIVE:?}"
: "${LC_WORKSPACE_NAME:?}" "${LC_PROJECT_ARCHIVE:?}" "${LC_TOOLCHAIN_ARCHIVE:?}"
: "${LC_CHECKSUMS:?}" "${LC_DATASET_REVISION:?}" "${LC_EXPECTED_ROWS:?}"
[[ "$LC_CODE_REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "$LC_WORKSPACE_NAME" =~ ^[A-Za-z0-9_-][A-Za-z0-9._-]*$ ]]
python_bin="$LC_REPO/.venv/bin/python"
git clone --no-hardlinks --quiet "$LC_REPO" "$TMPDIR/code"
git -C "$TMPDIR/code" checkout --quiet --detach "$LC_CODE_REVISION"
export PYTHONPATH="$TMPDIR/code/src"
cd "$TMPDIR/code"
"$python_bin" scripts/euler_quota.py "$LC_STAGE" --reserve-bytes 1073741824 --reserve-files 20
sha256sum -c "$LC_CHECKSUMS"
sha256sum -c "$LC_INPUT_ARCHIVE.sha256"
mkdir -p "$TMPDIR/project" "$TMPDIR/elan/toolchains"
tar -xf "$LC_PROJECT_ARCHIVE" -C "$TMPDIR/project"
tar -xf "$LC_TOOLCHAIN_ARCHIVE" -C "$TMPDIR/elan/toolchains"
tar -xf "$LC_INPUT_ARCHIVE" -C "$TMPDIR"
workspace="$TMPDIR/$LC_WORKSPACE_NAME"
[[ -d "$workspace/corpus" ]]
export ELAN_HOME="$TMPDIR/elan"
export LC_LOCAL_PROJECT="$TMPDIR/project" LC_LOCAL_WORKSPACE="$workspace"
"$python_bin" - <<'PY'
from pathlib import Path
import os
from lean_corpus.common import read_json, write_json
paths = list((Path(os.environ['LC_LOCAL_WORKSPACE']) / 'corpus/runtimes').glob('*.json'))
if len(paths) != 1:
    raise RuntimeError('this template requires exactly one explicitly prepared runtime')
runtime = read_json(paths[0])
runtime.update(project_dir=os.environ['LC_LOCAL_PROJECT'], runner='worker', memory_mb=8192,
               worker_max_cases=int(os.environ.get('LC_WORKER_MAX_CASES', '128')), parse_timeout=180)
write_json(paths[0], runtime)
PY
runner_pid=""
monitor_pid=""
checkpoint() {
  (
    trap - EXIT TERM INT USR1
    flock -x 9
    options=(--workspace "$workspace" --stage "$LC_STAGE" --job-id "$SLURM_JOB_ID" --phase "$1")
    if [[ "$#" -gt 1 ]]; then options+=(--runner-exit "$2"); fi
    "$python_bin" scripts/checkpoint_local.py "${options[@]}"
  ) 9>"$LC_STAGE/full-$SLURM_JOB_ID.lock"
}
finish() {
  status=$?
  trap - EXIT TERM INT USR1
  set +e
  if [[ -n "$runner_pid" ]] && kill -0 "$runner_pid" 2>/dev/null; then
    kill -TERM "$runner_pid"
    wait "$runner_pid"
  fi
  if [[ -n "$monitor_pid" ]]; then kill "$monitor_pid" 2>/dev/null; fi
  checkpoint final "$status"
  snapshot_status=$?
  if [[ "$snapshot_status" -ne 0 ]]; then exit "$snapshot_status"; fi
  exit "$status"
}
trap finish EXIT
trap 'exit 143' TERM INT USR1
extra=()
if [[ -n "${LC_LIMIT:-}" ]]; then extra+=(--limit "$LC_LIMIT"); fi
if [[ -n "${LC_SELECTION_FILE:-}" ]]; then
  [[ -z "${LC_LIMIT:-}" ]]
  selection=$(realpath -e "$workspace/$LC_SELECTION_FILE")
  [[ "$selection" == "$workspace/"* && -f "$selection" ]]
  extra+=(--selection "$selection")
fi
"$python_bin" scripts/run_local_corpus.py --work-dir "$workspace/corpus" \
  --output-dir "$workspace/full-export-$SLURM_JOB_ID" --source numina_math_lean \
  --dataset-revision "$LC_DATASET_REVISION" --expected-input-rows "$LC_EXPECTED_ROWS" \
  --timeout "${LC_TIMEOUT:-120}" "${extra[@]}" &
runner_pid=$!
(
  trap - EXIT TERM INT USR1
  while kill -0 "$runner_pid" 2>/dev/null; do
    sleep "${LC_CHECKPOINT_SECONDS:-900}"
    if kill -0 "$runner_pid" 2>/dev/null; then
      checkpoint periodic || printf 'Periodic checkpoint failed; previous generations retained\n' >&2
    fi
  done
) &
monitor_pid=$!
wait "$runner_pid"
