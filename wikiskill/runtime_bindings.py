"""Trusted runtime binding registry and operator-owned runtime state.

V0.3.1 keeps runtime execution policy out of proposal data. Operators may bind
an already-registered source to a fixed profile and activate one inference
source before baseline scoring. Executable profile details remain static code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from typing import Callable, Mapping

from . import gating, sources


class RuntimeBindingError(RuntimeError):
    """A trusted runtime could not be bound or executed safely."""


@dataclass(frozen=True)
class RuntimeBindingProfile:
    profile_id: str
    protocol: str
    timeout_s: int
    entrypoint: str


@dataclass
class BoundRuntimeSession:
    source_id: str
    source_sha: str
    profile_id: str
    fingerprint: str
    worktree: str
    runner: Callable
    evidence: dict
    _close_fn: Callable[[], dict] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_result: dict | None = field(default=None, init=False, repr=False)

    def close(self) -> dict:
        if self._closed:
            return dict(self._close_result or {"closed": True})
        result = self._close_fn()
        if not isinstance(result, dict):
            raise RuntimeBindingError("runtime binding close returned invalid evidence")
        value = {"closed": True, **result}
        self._closed = True
        self._close_result = value
        return dict(value)


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
_PUBLIC_RUNTIME_KEYS = ("source_id", "source_sha", "binding_profile", "fingerprint")


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
    if (
        os.path.isabs(profile.entrypoint)
        or "\\" in profile.entrypoint
        or any(part in ("", ".", "..") for part in profile.entrypoint.split("/"))
    ):
        raise ValueError(f"runtime binding profile entrypoint unsafe {profile_id!r}")
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
    return _identity(
        ws,
        active["source_id"],
        active["binding_profile"],
        registry=registry,
    )


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
        rows.append(
            _identity(ws, source_id, item["binding_profile"], registry=registry)
        )
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


def _git(repo: str, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=text,
        check=False,
    )


def _resolve_commit(repo: str, source_sha: str) -> str:
    if not isinstance(source_sha, str) or not source_sha:
        raise ValueError("runtime source SHA must be a full commit SHA")
    p = _git(repo, "rev-parse", "--verify", f"{source_sha}^{{commit}}")
    if p.returncode != 0:
        raise ValueError(f"runtime source SHA does not resolve: {source_sha}")
    resolved = p.stdout.strip()
    if resolved.lower() != source_sha.lower():
        raise ValueError(f"runtime source SHA must be the full commit SHA: {resolved}")
    return resolved


def _registration(
    ws: str,
    source_id: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> tuple[RuntimeBindingProfile, dict]:
    manifest = sources.get_manifest(ws, source_id)
    registrations = _load_registrations(ws, registry=registry)
    try:
        item = registrations["sources"][source_id]
    except KeyError:
        raise ValueError(f"source_id {source_id!r} has no runtime binding") from None
    return _profile(item["binding_profile"], registry), manifest


def _runtime_worktree_root(ws: str) -> str:
    return os.path.realpath(os.path.join(ws, "runs", "runtime-worktrees"))


def _core_worktree_root(ws: str) -> str:
    return os.path.realpath(os.path.join(ws, "runs", "core-worktrees"))


def _is_within(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _worktree_head(path: str) -> str:
    p = _git(path, "rev-parse", "HEAD")
    if p.returncode != 0:
        raise RuntimeBindingError(
            f"runtime worktree HEAD could not be resolved: {p.stderr.strip()}"
        )
    return p.stdout.strip()


def _entrypoint_evidence(
    repo: str,
    source_sha: str,
    worktree: str,
    profile: RuntimeBindingProfile,
) -> tuple[str, str]:
    p = _git(repo, "ls-tree", source_sha, "--", profile.entrypoint)
    if p.returncode != 0:
        raise RuntimeBindingError(
            f"runtime entrypoint tree lookup failed: {p.stderr.strip()}"
        )
    rows = [line for line in p.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError(
            f"runtime entrypoint is missing at bound SHA: {profile.entrypoint}"
        )
    try:
        left, shown = rows[0].split("\t", 1)
        mode, kind, blob_sha = left.split(" ", 2)
    except ValueError as exc:
        raise RuntimeBindingError("runtime entrypoint tree metadata is invalid") from exc
    if shown != profile.entrypoint or mode not in ("100644", "100755") or kind != "blob":
        raise ValueError(
            f"runtime entrypoint must be a tracked regular file: {profile.entrypoint}"
        )
    full = os.path.realpath(os.path.join(worktree, *profile.entrypoint.split("/")))
    root = os.path.realpath(worktree)
    if not _is_within(root, full) or os.path.islink(full) or not os.path.isfile(full):
        raise RuntimeBindingError("runtime entrypoint escaped or is not a regular file")
    try:
        data = open(full, "rb").read()
    except OSError as exc:
        raise RuntimeBindingError(f"runtime entrypoint cannot be read: {exc}") from exc
    if b"\x00" in data:
        raise ValueError("runtime entrypoint must be UTF-8 text")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("runtime entrypoint must be UTF-8 text") from exc
    hashed = _git(worktree, "hash-object", "--", profile.entrypoint)
    if hashed.returncode != 0 or hashed.stdout.strip() != blob_sha:
        raise RuntimeBindingError(
            "runtime entrypoint content does not match bound source SHA"
        )
    return full, blob_sha


def _assert_runtime_identity(
    worktree: str,
    source_sha: str,
    entrypoint: str,
    blob_sha: str,
) -> None:
    head = _worktree_head(worktree)
    if head != source_sha:
        raise RuntimeBindingError(
            f"runtime worktree HEAD SHA mismatch: expected {source_sha}, got {head}"
        )
    status = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        raise RuntimeBindingError("runtime worktree status could not be verified")
    if status.stdout.strip():
        raise RuntimeBindingError("runtime worktree has tracked changes after binding")
    hashed = _git(worktree, "hash-object", "--", entrypoint)
    if hashed.returncode != 0 or hashed.stdout.strip() != blob_sha:
        raise RuntimeBindingError("runtime entrypoint changed after binding")


def _public_evidence(evidence: dict) -> dict:
    return {key: evidence[key] for key in _PUBLIC_RUNTIME_KEYS}


def _make_python_runner(
    *,
    source_sha: str,
    worktree: str,
    entrypoint_abs: str,
    entrypoint_rel: str,
    entrypoint_blob_sha: str,
    profile: RuntimeBindingProfile,
    evidence: dict,
) -> Callable:
    def runner(
        ws: str,
        prompt: str,
        *,
        tag: str,
        workdir: str | None = None,
        model: str | None = None,
        dry_run: bool = False,
        max_turns: int = 15,
        **kwargs,
    ) -> dict:
        del ws, model, kwargs
        if not isinstance(workdir, str) or not os.path.isdir(workdir):
            raise RuntimeBindingError("runtime task sandbox workdir does not exist")
        _assert_runtime_identity(
            worktree, source_sha, entrypoint_rel, entrypoint_blob_sha
        )
        request = {
            "protocol": profile.protocol,
            "prompt": prompt,
            "tag": tag,
            "workdir": os.path.realpath(workdir),
            "max_turns": max_turns,
        }
        if dry_run:
            return {
                "cmd": [sys.executable, entrypoint_abs],
                "dry_run": True,
                "runtime": _public_evidence(evidence),
            }
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, entrypoint_abs],
                cwd=worktree,
                input=json.dumps(request, separators=(",", ":")) + "\n",
                capture_output=True,
                text=True,
                timeout=profile.timeout_s,
                shell=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONIOENCODING": "utf-8",
                },
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeBindingError(
                f"runtime timed out after {profile.timeout_s}s"
            ) from exc
        except OSError as exc:
            raise RuntimeBindingError(f"runtime process could not launch: {exc}") from exc
        duration = round(time.monotonic() - started, 4)
        if proc.returncode != 0:
            raise RuntimeBindingError(
                f"runtime process exit code {proc.returncode}: {(proc.stderr or '')[-1000:]}"
            )
        output = (proc.stdout or "").strip()
        lines = output.splitlines()
        if len(lines) != 1:
            raise RuntimeBindingError(
                "runtime response must contain exactly one JSON object"
            )
        try:
            response = json.loads(lines[0])
        except ValueError as exc:
            raise RuntimeBindingError("runtime response is not valid JSON") from exc
        if not isinstance(response, dict) or set(response) != {"status"}:
            raise RuntimeBindingError("runtime JSON response has invalid fields")
        if response.get("status") != "ok":
            raise RuntimeBindingError(
                f"runtime response status is not ok: {response.get('status')!r}"
            )
        return {
            "exit_code": 0,
            "duration_s": duration,
            "stdout_path": None,
            "session_file": None,
            "runtime": _public_evidence(evidence),
        }

    return runner


def bind_sha(
    ws: str,
    source_id: str,
    source_sha: str,
    *,
    candidate_worktree: str | None = None,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> BoundRuntimeSession:
    profile, manifest = _registration(ws, source_id, registry=registry)
    repo = manifest["repository"]
    source_sha = _resolve_commit(repo, source_sha)
    owns_worktree = candidate_worktree is None

    if candidate_worktree is None:
        root = _runtime_worktree_root(ws)
        os.makedirs(root, exist_ok=True)
        name = f"accepted-{source_id}-{source_sha[:12]}-{secrets.token_hex(4)}"
        worktree = os.path.realpath(os.path.join(root, name))
        if not _is_within(root, worktree) or os.path.exists(worktree):
            raise RuntimeBindingError("generated runtime worktree path is unsafe")
        p = _git(repo, "worktree", "add", "--detach", worktree, source_sha)
        if p.returncode != 0:
            raise RuntimeBindingError(
                f"accepted runtime worktree could not be created: {p.stderr.strip()}"
            )
    else:
        root = _core_worktree_root(ws)
        worktree = os.path.realpath(candidate_worktree)
        if not _is_within(root, worktree) or not os.path.isdir(worktree):
            raise RuntimeBindingError(
                "candidate runtime worktree is outside the core worktree root"
            )

    try:
        head = _worktree_head(worktree)
        if head != source_sha:
            raise RuntimeBindingError(
                f"runtime worktree HEAD SHA mismatch: expected {source_sha}, got {head}"
            )
        entrypoint_abs, blob_sha = _entrypoint_evidence(
            repo, source_sha, worktree, profile
        )
        fingerprint = runtime_fingerprint(
            source_id, profile.profile_id, source_sha, profile.protocol
        )
        evidence = {
            "source_id": source_id,
            "source_sha": source_sha,
            "binding_profile": profile.profile_id,
            "fingerprint": fingerprint,
            "protocol": profile.protocol,
            "worktree_head": head,
            "entrypoint_blob_sha": blob_sha,
        }
        runner = _make_python_runner(
            source_sha=source_sha,
            worktree=worktree,
            entrypoint_abs=entrypoint_abs,
            entrypoint_rel=profile.entrypoint,
            entrypoint_blob_sha=blob_sha,
            profile=profile,
            evidence=evidence,
        )
    except Exception:
        if owns_worktree:
            p = _git(repo, "worktree", "remove", "--force", worktree)
            _git(repo, "worktree", "prune")
            if p.returncode != 0 and os.path.exists(worktree):
                raise RuntimeBindingError(
                    "runtime binding failed and accepted worktree cleanup also failed"
                )
        raise

    def close_impl() -> dict:
        if not owns_worktree:
            return {"worktree_removed": False, "candidate_worktree_owned": False}
        if not os.path.exists(worktree):
            _git(repo, "worktree", "prune")
            return {"worktree_removed": True, "already_missing": True}
        p = _git(repo, "worktree", "remove", "--force", worktree)
        if p.returncode != 0:
            raise RuntimeBindingError(
                f"accepted runtime worktree could not be removed: {p.stderr.strip()}"
            )
        prune = _git(repo, "worktree", "prune")
        if prune.returncode != 0:
            raise RuntimeBindingError(
                f"accepted runtime worktree could not be pruned: {prune.stderr.strip()}"
            )
        if os.path.exists(worktree):
            raise RuntimeBindingError("accepted runtime worktree remained after close")
        return {"worktree_removed": True}

    return BoundRuntimeSession(
        source_id=source_id,
        source_sha=source_sha,
        profile_id=profile.profile_id,
        fingerprint=fingerprint,
        worktree=worktree,
        runner=runner,
        evidence=evidence,
        _close_fn=close_impl,
    )


def bind_active_accepted(
    ws: str,
    *,
    registry: Mapping[str, RuntimeBindingProfile] | None = None,
) -> BoundRuntimeSession | None:
    active = active_runtime_config(ws, registry=registry)
    if active is None:
        return None
    return bind_sha(
        ws,
        active["source_id"],
        active["accepted_sha"],
        registry=registry,
    )
