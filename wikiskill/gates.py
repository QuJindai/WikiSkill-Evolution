"""Trusted engineering-gate profiles for governed source evolution.

Production proposals may reference profile ids, but may never supply commands.
All executable argv is defined in this module or injected explicitly by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
import sys
import time
from typing import Mapping


class GateOperationalError(RuntimeError):
    """The gate runner could not execute a trusted profile."""


@dataclass(frozen=True)
class GateProfile:
    profile_id: str
    argv: tuple[str, ...]
    timeout_s: int = 600


PROFILES: dict[str, GateProfile] = {
    "registered:wikiskill-static": GateProfile(
        "registered:wikiskill-static",
        (sys.executable, "-m", "pyflakes", "wikiskill/", "tests/"),
        300,
    ),
    "registered:wikiskill-build": GateProfile(
        "registered:wikiskill-build",
        (sys.executable, "-m", "compileall", "-q", "wikiskill"),
        300,
    ),
    "registered:wikiskill-regression": GateProfile(
        "registered:wikiskill-regression",
        (sys.executable, "-m", "pytest", "tests/", "-q"),
        900,
    ),
}


def _registry(registry: Mapping[str, GateProfile] | None) -> Mapping[str, GateProfile]:
    return PROFILES if registry is None else registry


def has_profile(profile_id: str, registry: Mapping[str, GateProfile] | None = None) -> bool:
    return profile_id in _registry(registry)


def _summary(stdout: str, stderr: str) -> str:
    text = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    return text[-4000:]


def run_profile(
    gate: str,
    profile_id: str,
    cwd: str,
    registry: Mapping[str, GateProfile] | None = None,
) -> dict:
    profiles = _registry(registry)
    try:
        profile = profiles[profile_id]
    except KeyError:
        raise ValueError(f"unknown registered gate profile {profile_id!r}") from None
    if not isinstance(profile, GateProfile):
        raise ValueError(f"invalid registered gate profile {profile_id!r}")
    if profile.profile_id != profile_id:
        raise ValueError(f"registered gate profile id mismatch {profile_id!r}")
    if not profile.argv or not all(isinstance(arg, str) and arg for arg in profile.argv):
        raise ValueError(f"registered gate profile argv invalid {profile_id!r}")
    if isinstance(profile.timeout_s, bool) or not isinstance(profile.timeout_s, int) or profile.timeout_s < 1:
        raise ValueError(f"registered gate profile timeout invalid {profile_id!r}")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(profile.argv),
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=profile.timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateOperationalError(
            f"registered gate profile {profile_id!r} could not run: {exc}"
        ) from exc

    return {
        "gate": gate,
        "profile": profile_id,
        "status": "pass" if proc.returncode == 0 else "fail",
        "exit_code": proc.returncode,
        "duration_s": round(time.monotonic() - started, 4),
        "summary": _summary(proc.stdout or "", proc.stderr or ""),
        "evidence": {"artifact_refs": [], "metrics": {}},
    }
