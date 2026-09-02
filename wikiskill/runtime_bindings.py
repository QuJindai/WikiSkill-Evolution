"""Trusted runtime binding registry and operator-owned runtime state.

V0.3.1 keeps runtime execution policy out of proposal data. Operators may bind
an already-registered source to a fixed profile and activate one inference
source before baseline scoring. Executable profile details remain static code.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Mapping

from . import gating, sources


class RuntimeBindingError(RuntimeError):
    """A trusted runtime could not be bound or executed safely."""


@dataclass(frozen=True)
class RuntimeBindingProfile:
    profile_id: str
    protocol: str
    timeout_s: int
    entrypoint: str


PROFILES: dict[str, RuntimeBindingProfile] = {
    "registered:python-json-runner-v1": RuntimeBindingProfile(
        profile_id="registered:python-json-runner-v1",
        protocol="wikiskill-runtime-v1",
        timeout_s=30,
        entrypoint="wikiskill_runtime.py",
    )
}

_REGISTRATION_KEYS = {"binding_profile", "role"}
_ACTIVE_KEYS = {"version", "role", "source_id", "binding_profile"}


def _registry(
    registry: Mapping[str, RuntimeBindingProfile] | None,
) -> Mapping[str, RuntimeBindingProfile]:
    return PROFILES if registry is None else registry


def _profile(
    profile_id: str,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> RuntimeBindingProfile:
    profiles = _registry(registry)
    try:
        profile = profiles[profile_id]
    except KeyError:
        raise ValueError(f"unknown runtime binding profile {profile_id!r}") from None
    if not isinstance(profile, RuntimeBindingProfile):
        raise ValueError(f"invalid runtime binding profile {profile_id!r}")
    if profile.profile_id != profile_id:
        raise ValueError(f"runtime binding profile id mismatch {profile_id!r}")
    if profile.protocol != "wikiskill-runtime-v1":
        raise ValueError(f"unsupported runtime binding protocol {profile.protocol!r}")
    if isinstance(profile.timeout_s, bool) or not isinstance(profile.timeout_s, int):
        raise ValueError(f"runtime binding profile timeout invalid {profile_id!r}")
    if profile.timeout_s < 1:
        raise ValueError(f"runtime binding profile timeout invalid {profile_id!r}")
    if not isinstance(profile.entrypoint, str) or not profile.entrypoint:
        raise ValueError(f"runtime binding profile entrypoint invalid {profile_id!r}")
    return profile


def runtime_fingerprint(
    source_id: str,
    profile_id: str,
    source_sha: str,
    protocol: str,
) -> str:
    payload = {
        "binding_profile": profile_id,
        "protocol": protocol,
        "source_id": source_id,
        "source_sha": source_sha,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _runtime_dir(ws: str) -> str:
    return os.path.join(ws, "runtime")


def _registrations_path(ws: str) -> str:
    return os.path.join(_runtime_dir(ws), "registrations.json")


def _active_path(ws: str) -> str:
    return os.path.join(_runtime_dir(ws), "active.json")


def _atomic_json(path: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _load_json(path: str, *, label: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected object")
    return value


def _load_registrations(
    ws: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict:
    path = _registrations_path(ws)
    if not os.path.exists(path):
        return {"version": 1, "sources": {}}
    value = _load_json(path, label="runtime registrations")
    if set(value) != {"version", "sources"} or value.get("version") != 1:
        raise ValueError("invalid runtime registrations: expected version and sources")
    items = value.get("sources")
    if not isinstance(items, dict):
        raise ValueError("invalid runtime registrations: sources must be an object")
    normalized = {}
    for source_id, item in items.items():
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("invalid runtime registrations: source id")
        if not isinstance(item, dict) or set(item) != _REGISTRATION_KEYS:
            raise ValueError(
                f"invalid runtime registrations: registration for {source_id!r}"
            )
        if item.get("role") != "inference":
            raise ValueError("invalid runtime registrations: role must be inference")
        profile_id = item.get("binding_profile")
        if not isinstance(profile_id, str):
            raise ValueError("invalid runtime registrations: binding profile")
        _profile(profile_id, registry)
        normalized[source_id] = {
            "binding_profile": profile_id,
            "role": "inference",
        }
    return {"version": 1, "sources": normalized}


def _load_active(
    ws: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict | None:
    path = _active_path(ws)
    if not os.path.exists(path):
        return None
    value = _load_json(path, label="active runtime")
    if set(value) != _ACTIVE_KEYS or value.get("version") != 1:
        raise ValueError("invalid active runtime configuration")
    if value.get("role") != "inference":
        raise ValueError("invalid active runtime role")
    source_id = value.get("source_id")
    profile_id = value.get("binding_profile")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("invalid active runtime source_id")
    if not isinstance(profile_id, str):
        raise ValueError("invalid active runtime binding_profile")
    _profile(profile_id, registry)
    return {
        "version": 1,
        "role": "inference",
        "source_id": source_id,
        "binding_profile": profile_id,
    }


def bind_source(
    ws: str,
    source_id: str,
    profile_id: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict:
    sources.get_manifest(ws, source_id)
    _profile(profile_id, registry)
    registrations = _load_registrations(ws, registry=registry)
    registrations["sources"][source_id] = {
        "binding_profile": profile_id,
        "role": "inference",
    }
    _atomic_json(_registrations_path(ws), registrations)
    return {
        "source_id": source_id,
        "binding_profile": profile_id,
        "role": "inference",
    }


def _identity(
    ws: str,
    source_id: str,
    profile_id: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict:
    profile = _profile(profile_id, registry)
    sha = sources.accepted_sha(ws, source_id)
    return {
        "source_id": source_id,
        "binding_profile": profile_id,
        "accepted_sha": sha,
        "fingerprint": runtime_fingerprint(
            source_id, profile_id, sha, profile.protocol
        ),
        "role": "inference",
    }


def activate_source(
    ws: str,
    source_id: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict:
    state = gating.load_state(ws)
    if state.get("baseline") is not None:
        raise ValueError("runtime activation requires an unscored workspace")
    registrations = _load_registrations(ws, registry=registry)
    try:
        registration = registrations["sources"][source_id]
    except KeyError:
        raise ValueError(f"source_id {source_id!r} has no runtime binding") from None
    sources.get_manifest(ws, source_id)
    profile_id = registration["binding_profile"]
    _profile(profile_id, registry)
    active = {
        "version": 1,
        "role": "inference",
        "source_id": source_id,
        "binding_profile": profile_id,
    }
    _atomic_json(_active_path(ws), active)
    return _identity(ws, source_id, profile_id, registry=registry)


def active_runtime_config(
    ws: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict | None:
    active = _load_active(ws, registry=registry)
    if active is None:
        return None
    registrations = _load_registrations(ws, registry=registry)
    registration = registrations["sources"].get(active["source_id"])
    if registration is None:
        raise ValueError("active runtime source is not registered")
    if registration["binding_profile"] != active["binding_profile"]:
        raise ValueError("active runtime profile does not match runtime registration")
    sources.get_manifest(ws, active["source_id"])
    identity = _identity(
        ws,
        active["source_id"],
        active["binding_profile"],
        registry=registry,
    )
    return identity


def inspect_runtime(
    ws: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict:
    registrations = _load_registrations(ws, registry=registry)
    rows = []
    for source_id in sorted(registrations["sources"]):
        item = registrations["sources"][source_id]
        sources.get_manifest(ws, source_id)
        identity = _identity(
            ws, source_id, item["binding_profile"], registry=registry
        )
        rows.append(identity)
    return {
        "version": 1,
        "registrations": rows,
        "active": active_runtime_config(ws, registry=registry),
    }


def validate_runtime(
    ws: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> dict:
    inspected = inspect_runtime(ws, registry=registry)
    return {
        "status": "valid",
        "version": 1,
        "registrations": inspected["registrations"],
        "active": inspected["active"],
    }
