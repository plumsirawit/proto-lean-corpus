"""Fail before writing when Euler's byte/file soft quotas lack the requested margin."""

import argparse
import csv
import io
from pathlib import Path
import subprocess


def check(path, reserve_bytes=0, reserve_files=0, fraction=0.8):
    if reserve_bytes < 0 or reserve_files < 0 or not 0 < fraction < 1:
        raise ValueError(
            "nonnegative reserves and a fraction between zero and one are required"
        )
    target = Path(path).resolve()
    # Query the destination filesystem; unrelated home quota RPC failures must not
    # block a scratch checkpoint. Any failure for this destination still aborts.
    output = subprocess.check_output(
        ["lquota", "-b", "-P", str(target)], text=True, timeout=30
    )
    rows = list(csv.DictReader(io.StringIO(output), delimiter="|"))
    matching = [
        r for r in rows if target.is_relative_to(Path(r["Storage location"].strip()))
    ]
    if not matching:
        raise RuntimeError(f"no quota information for {target}")
    longest = max(len(r["Storage location"]) for r in matching)
    found = set()
    for row in matching:
        if len(row["Storage location"]) != longest:
            continue
        kind = row["Quota type"].strip()
        used, soft = int(row["Used"]), int(row["Soft quota"])
        reserve = reserve_bytes if kind == "space" else reserve_files
        if soft <= 0 or used + reserve > soft * fraction:
            raise RuntimeError(
                f"quota margin exceeded: {kind} used={used} reserve={reserve} soft={soft}"
            )
        found.add(kind)
    if found != {"space", "files"}:
        raise RuntimeError("both space and file quotas are required")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--reserve-bytes", type=int, default=0)
    parser.add_argument("--reserve-files", type=int, default=0)
    args = parser.parse_args()
    check(args.path, args.reserve_bytes, args.reserve_files)
