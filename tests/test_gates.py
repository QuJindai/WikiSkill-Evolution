"""Trusted registered gate profiles for V0.3 source evolution."""

import sys

import pytest

from wikiskill import gates


def test_known_profile_returns_structured_pass(tmp_path):
    registry = {
        "registered:test-pass": gates.GateProfile(
            "registered:test-pass",
            (sys.executable, "-c", "print('ok')"),
            30,
        )
    }
    result = gates.run_profile(
        "build", "registered:test-pass", str(tmp_path), registry
    )
    assert result["gate"] == "build"
    assert result["profile"] == "registered:test-pass"
    assert result["status"] == "pass"
    assert result["exit_code"] == 0
    assert result["duration_s"] >= 0
    assert "ok" in result["summary"]
    assert result["evidence"] == {"artifact_refs": [], "metrics": {}}


def test_unknown_profile_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="unknown registered gate profile"):
        gates.run_profile("build", "registered:missing", str(tmp_path), {})


def test_nonzero_profile_is_gate_fail_not_exception(tmp_path):
    registry = {
        "registered:test-fail": gates.GateProfile(
            "registered:test-fail",
            (sys.executable, "-c", "raise SystemExit(7)"),
            30,
        )
    }
    result = gates.run_profile(
        "regression", "registered:test-fail", str(tmp_path), registry
    )
    assert result["status"] == "fail"
    assert result["exit_code"] == 7


def test_process_launch_failure_is_operational_error(tmp_path):
    registry = {
        "registered:missing-bin": gates.GateProfile(
            "registered:missing-bin",
            ("__wikiskill_missing_executable__",),
            30,
        )
    }
    with pytest.raises(gates.GateOperationalError):
        gates.run_profile(
            "static", "registered:missing-bin", str(tmp_path), registry
        )


def test_summary_is_bounded(tmp_path):
    registry = {
        "registered:long": gates.GateProfile(
            "registered:long",
            (sys.executable, "-c", "print('x' * 6000)"),
            30,
        )
    }
    result = gates.run_profile("static", "registered:long", str(tmp_path), registry)
    assert len(result["summary"]) <= 4000
