"""Fail-closed Linux user/mount/network namespace worker with read-only inputs."""

import ctypes
import json
import os
from pathlib import Path
import sys


def sandbox(spec):
    libc = ctypes.CDLL(None, use_errno=True)

    def mount(source, target, filesystem=None, flags=0, data=None):
        args = [
            x.encode() if isinstance(x, str) else x
            for x in (source, target, filesystem)
        ]
        if libc.mount(*args, ctypes.c_ulong(flags), data) != 0:
            raise OSError(ctypes.get_errno(), f"mount {target}")

    # Namespace created by unshare. Make all mount changes private.
    mount(None, "/", flags=(1 << 18) | (1 << 14))
    root = Path(spec["write"]) / "rootfs"
    root.mkdir()
    mount("tmpfs", str(root), "tmpfs", data=b"size=256m")
    # Parent mounts first. Bind (not recursive bind) avoids inherited writable mounts.
    reads = sorted(
        set(os.path.realpath(p) for p in spec["reads"] if os.path.exists(p)),
        key=lambda p: (len(Path(p).parts), p),
    )
    for src in reads:
        dest = root / src.lstrip("/")
        if any(src.startswith(parent + "/") for parent in reads if parent != src):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if os.path.isdir(src):
            dest.mkdir(exist_ok=True)
        else:
            dest.touch()
        mount(src, str(dest), flags=4096)
        mount(
            None,
            str(dest),
            flags=4096 | 32 | 1 | 2 | (0 if src.startswith("/dev/") else 4),
        )  # bind,remount,ro,nosuid,nodev
    # Recreate standard merged-/usr links where the host uses them.
    for name in ("bin", "lib", "lib64"):
        host = Path("/") / name
        dest = root / name
        if host.is_symlink() and not dest.exists():
            dest.symlink_to(os.readlink(host))
    (root / "proc").mkdir(exist_ok=True)
    mount("proc", str(root / "proc"), "proc", flags=2 | 4 | 8)
    (root / "tmp").mkdir(exist_ok=True)
    source = Path(spec["command"][-3]).read_text()
    (root / "tmp" / "Task.lean").write_text(source)
    command = spec["command"][:]
    command[-3] = "/tmp/Task.lean"
    os.chroot(root)
    os.chdir(spec["cwd"])
    os.execve(command[0], command, spec["env"])


if __name__ == "__main__":
    try:
        sandbox(json.loads(sys.argv[1]))
    except Exception as e:
        print(f"CORPUS_SANDBOX_ERROR:{e}", file=sys.stderr)
        sys.exit(125)
