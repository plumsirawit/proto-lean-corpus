#!/bin/bash
#SBATCH --job-name=lean-pilot
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem-per-cpu=8G
#SBATCH --tmp=40G
#SBATCH --time=03:00:00
#SBATCH --signal=B:USR1@180
# Already acquired inputs and built dependencies only. No downloads or uploads.
set -euo pipefail
umask 077
: "${SLURM_JOB_ID:?}" "${TMPDIR:?}" "${LC_REPO:?}" "${LC_STAGE:?}"
: "${LC_SOURCE:?}" "${LC_PILOT_SIZE:?}" "${LC_CODE_REVISION:?}"
: "${LC_PROJECT_ARCHIVE:?}" "${LC_TOOLCHAIN_ARCHIVE:?}" "${LC_CHECKSUMS:?}"
: "${LC_INPUT_ARCHIVE:?}"
case "$LC_SOURCE" in goedel_pset_v1|lean_workbook|putnam_bench) ;; *) exit 2 ;; esac
case "$LC_PILOT_SIZE" in 10|100|1000) ;; *) exit 2 ;; esac
[[ "$LC_CODE_REVISION" =~ ^[0-9a-f]{40}$ ]]
python_bin="$LC_REPO/.venv/bin/python"
git clone --no-hardlinks --quiet "$LC_REPO" "$TMPDIR/code"
git -C "$TMPDIR/code" checkout --quiet --detach "$LC_CODE_REVISION"
export PYTHONPATH="$TMPDIR/code/src"
cd "$TMPDIR/code"
"$python_bin" scripts/euler_quota.py "$LC_STAGE" --reserve-bytes 2147483648 --reserve-files 20
sha256sum -c "$LC_CHECKSUMS"
sha256sum -c "$LC_INPUT_ARCHIVE.sha256"
mkdir -p "$TMPDIR/project" "$TMPDIR/elan"
tar -xf "$LC_PROJECT_ARCHIVE" -C "$TMPDIR/project"
tar -xf "$LC_TOOLCHAIN_ARCHIVE" -C "$TMPDIR/elan"
tar -xf "$LC_INPUT_ARCHIVE" -C "$TMPDIR"
workspace="$TMPDIR/$LC_SOURCE"
[[ -d "$workspace/corpus" ]]
export ELAN_HOME="$TMPDIR/elan" LC_LOCAL_PROJECT="$TMPDIR/project"
export LC_LOCAL_WORKSPACE="$workspace"
checking_chain=$(cat "$TMPDIR/project/lean-toolchain")
checking_directory=${checking_chain//\//--}
checking_directory=${checking_directory//:/---}
export PATH="$ELAN_HOME/toolchains/$checking_directory/bin:$PATH"
export LEAN_NUM_THREADS="${SLURM_CPUS_PER_TASK:-2}"
runner_pid=""
finish() {
  status=$?
  trap - EXIT TERM INT USR1
  set +e
  if [[ -n "$runner_pid" ]] && kill -0 "$runner_pid" 2>/dev/null; then
    kill -TERM "$runner_pid"
    wait "$runner_pid"
  fi
  archive="$LC_STAGE/pilot-$LC_SOURCE-$LC_PILOT_SIZE-$SLURM_JOB_ID.tar"
  "$python_bin" scripts/euler_quota.py "$LC_STAGE" --reserve-bytes 2147483648 --reserve-files 10
  quota_status=$?
  if [[ "$quota_status" -ne 0 ]]; then exit "$quota_status"; fi
  tar -cf "$archive.partial" -C "$TMPDIR" "$LC_SOURCE"
  archive_status=$?
  if [[ "$archive_status" -ne 0 ]]; then exit "$archive_status"; fi
  mv "$archive.partial" "$archive"
  sha256sum "$archive" > "$archive.sha256"
  export LC_FINAL_ARCHIVE="$archive" LC_RUNNER_EXIT="$status"
  "$python_bin" - <<'PY'
from pathlib import Path
import json, os
from lean_corpus.common import file_hash, write_json
archive = Path(os.environ['LC_FINAL_ARCHIVE'])
result = Path(os.environ['LC_LOCAL_WORKSPACE']) / 'results' / ('pilot-' + os.environ['LC_PILOT_SIZE']) / 'pilot-result.json'
report = json.loads(result.read_text()) if result.exists() else {}
write_json(Path(str(archive) + '.json'), {
    'archive': str(archive), 'sha256': file_hash(archive),
    'runner_exit': int(os.environ['LC_RUNNER_EXIT']), 'pilot': report,
    'complete': os.environ['LC_RUNNER_EXIT'] == '0' and report.get('complete', False),
    'source': os.environ['LC_SOURCE'], 'size': int(os.environ['LC_PILOT_SIZE']),
    'code_revision': os.environ['LC_CODE_REVISION'], 'local_only': True,
})
PY
  metadata_status=$?
  if [[ "$metadata_status" -ne 0 ]]; then exit "$metadata_status"; fi
  exit "$status"
}
trap finish EXIT
trap 'exit 143' TERM INT USR1
# Historical Mathlib caches can omit the top-level import module. Complete it
# offline on the allocated node, using only the already pinned dependencies.
if [[ -f "$TMPDIR/project/Mathlib.lean" && ! -f "$TMPDIR/project/.lake/build/lib/Mathlib.olean" && ! -f "$TMPDIR/project/.lake/build/lib/lean/Mathlib.olean" ]]; then
  (cd "$TMPDIR/project"; lake build Mathlib)
fi
"$python_bin" - <<'PY'
from pathlib import Path
import os
from lean_corpus.acquire import selected_snapshots
from lean_corpus.environment import resolve_environment
from lean_corpus.common import read_json, write_json
work = Path(os.environ['LC_LOCAL_WORKSPACE']) / 'corpus'
project = Path(os.environ['LC_LOCAL_PROJECT'])
snapshot, _ = next(iter(selected_snapshots(work, [os.environ['LC_SOURCE']])))
paths = [p for p in (project / '.lake/build/lib/lean', project / '.lake/build/lib') if p.is_dir()]
for package in sorted((project / '.lake/packages').iterdir()):
    paths.extend(p for p in (package / '.lake/build/lib/lean', package / '.lake/build/lib') if p.is_dir())
resolution = resolve_environment(work, snapshot.name, project, [str(p.relative_to(project)) for p in paths])
runtime_file = work / 'runtimes' / (resolution['environment_id'] + '.json')
runtime = read_json(runtime_file)
runtime.update(runner='worker', memory_mb=int(os.environ.get('LC_CHECKER_MEMORY_MB', '8192')), worker_max_cases=128, parse_timeout=180)
write_json(runtime_file, runtime)
PY
extra=()
if [[ "$LC_SOURCE" != putnam_bench ]]; then
  extra+=(--selection "$workspace/selection-$LC_PILOT_SIZE.json")
fi
srun --cpu-bind=cores --ntasks=1 --cpus-per-task="${SLURM_CPUS_PER_TASK:-2}" \
  "$python_bin" scripts/run_local_pilots.py --work-dir "$workspace/corpus" \
  --output-dir "$workspace/results/pilot-$LC_PILOT_SIZE" --source "$LC_SOURCE" \
  --size "$LC_PILOT_SIZE" --workers "${LC_VERIFY_WORKERS:-2}" "${extra[@]}" &
runner_pid=$!
wait "$runner_pid"
runner_pid=""
