"""Transient RPC failures never replace a successful destination quota check."""

import runpy
import subprocess
import time

import pytest


def test_quota_retries_rpc_then_still_enforces_margin(tmp_path, monkeypatch):
    check = runpy.run_path("scripts/euler_quota.py")["check"]
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.TimeoutExpired(command, 30)
        return (
            "Storage location|Quota type|Used|Soft quota|Hard quota|\n"
            f"{tmp_path}|space|100|1000|1100|\n"
            f"{tmp_path}|files|10|1000|1100|\n"
        )

    monkeypatch.setattr(subprocess, "check_output", query)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    check(tmp_path, reserve_bytes=500, reserve_files=20)
    assert len(calls) == 3
    with pytest.raises(RuntimeError, match="quota margin exceeded"):
        check(tmp_path, reserve_bytes=701)
    assert len(calls) == 4  # A real margin rejection is not retried.


def test_quota_stays_closed_when_rpc_never_recovers(tmp_path, monkeypatch):
    check = runpy.run_path("scripts/euler_quota.py")["check"]
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(subprocess, "check_output", query)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(subprocess.TimeoutExpired):
        check(tmp_path)
    assert len(calls) == 3


def test_quota_preserves_rpc_failure_diagnostic(tmp_path, monkeypatch):
    check = runpy.run_path("scripts/euler_quota.py")["check"]

    def query(command, **kwargs):
        raise subprocess.CalledProcessError(
            5, command, output="Lustre quota temporarily unavailable"
        )

    monkeypatch.setattr(subprocess, "check_output", query)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="Lustre quota temporarily unavailable"):
        check(tmp_path)


def test_checkpoint_quota_wait_requires_fresh_success(tmp_path, monkeypatch):
    wait = runpy.run_path("scripts/euler_quota.py")["wait_for_quota"]
    clock = [0]
    calls = []

    def query(path, reserve_bytes, reserve_files):
        calls.append((path, reserve_bytes, reserve_files))
        if len(calls) < 3:
            raise RuntimeError("quota margin exceeded: space")

    monkeypatch.setitem(wait.__globals__, "check", query)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    wait(tmp_path, 500, 20, wait_seconds=90)
    assert calls == [(tmp_path, 500, 20)] * 3
    assert clock[0] == 60


def test_checkpoint_quota_wait_expires_closed(tmp_path, monkeypatch):
    wait = runpy.run_path("scripts/euler_quota.py")["wait_for_quota"]
    clock = [0]
    calls = []

    def query(*args):
        calls.append(args)
        raise subprocess.TimeoutExpired("lquota", 30)

    monkeypatch.setitem(wait.__globals__, "check", query)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    with pytest.raises(subprocess.TimeoutExpired):
        wait(tmp_path, 500, 20, wait_seconds=45)
    assert len(calls) == 3 and clock[0] == 45


def test_quota_path_lookup_is_bounded_before_rpc(tmp_path, monkeypatch):
    check = runpy.run_path("scripts/euler_quota.py")["check"]
    calls = []

    def resolve(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", resolve)
    monkeypatch.setattr(
        subprocess, "check_output",
        lambda *a, **k: pytest.fail("unresolved destination must not query quota"),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        check(tmp_path)
    assert len(calls) == 1 and calls[0][1]["timeout"] == 10
    assert calls[0][0][-1] == str(tmp_path)


def test_quota_symlink_uses_resolved_filesystem(tmp_path, monkeypatch):
    check = runpy.run_path("scripts/euler_quota.py")["check"]
    destination = tmp_path / "destination"
    destination.mkdir()
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        return (
            "Storage location|Quota type|Used|Soft quota|Hard quota|\n"
            f"{destination}|space|100|1000|1100|\n"
            f"{destination}|files|10|1000|1100|\n"
        )

    monkeypatch.setattr(subprocess, "check_output", query)
    check(link, reserve_bytes=500, reserve_files=20)
    assert calls[0][-1] == str(destination)
