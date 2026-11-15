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
