"""Trusted source registry and accepted Git-ref state for V0.3.

The operator-authored manifest defines repository identity and policy. Runtime
accepted state is authoritative in refs/wikiskill/<source_id>/accepted; JSON in
the WikiSkill workspace is a repairable audit mirror only.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import subprocess
from typing import Mapping

from . import gates


class SourceOperationalError(RuntimeError):
    """Trusted source state could not be read or advanced safely."""


MANIFEST_KEYS = {
    "source_id",
    "adapter",
    "repository",
    "baseline_ref",
    "baseline_sha",
    "write_policy",
    "patch_policy",
    "gates",
}
WRITE_POLICY_KEYS = {"allow", "deny"}
PATCH_POLICY_KEYS = {"max_files", "max_total_lines", "text_only"}
GATE_KEYS = {"static", "build", "regression", "performance"}
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sources_dir(ws: str) -> str:
    return os.path.join(ws, "sources")


def _registry_path(ws: str) -> str:
    return os.path.join(_sources_dir(ws), "registry.json")


def _manifest_path(ws: str, source_id: str) -> str:
    return os.path.join(_sources_dir(ws), "manifests", f"{source_id}.json")


def _state_path(ws: str, source_id: str) -> str:
    return os.path.join(_sources_dir(ws), "state", f"{source_id}.json")


def _ensure_dirs(ws: str) -> None:
    for name in ("manifests", "state"):
        os.makedirs(os.path.join(_sources_dir(ws), name), exist_ok=True)


def _atomic_json(path: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _require_git_repo(repo: str) -> str:
    real = os.path.realpath(os.path.abspath(repo))
    if not os.path.isdir(real):
        raise ValueError(f"source repository does not exist: {repo}")
    p = _git(real, "rev-parse", "--git-dir")
    if p.returncode != 0:
        raise ValueError(f"source repository is not a Git repository: {repo}")
    return real


def _resolve_commit(repo: str, value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a full commit SHA")
    p = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
    if p.returncode != 0:
        raise ValueError(f"{label} does not resolve to a commit: {value}")
    sha = p.stdout.strip()
    if value.lower() != sha.lower():
        raise ValueError(f"{label} must be the full commit SHA: {sha}")
    return sha


def _validate_patterns(values, *, label: str, require_nonempty: bool) -> list[str]:
    if not isinstance(values, list) or (require_nonempty and not values):
        raise ValueError(f"{label} must be {'a non-empty' if require_nonempty else 'an'} list")
    out = []
    for item in values:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label} entries must be non-empty strings")
        normalized = item.replace("\\", "/")
        if normalized.startswith("/") or any(part == ".." for part in normalized.split("/")):
            raise ValueError(f"unsafe source path pattern {item!r}")
        out.append(normalized)
    return out


def normalize_manifest(
    raw: dict,
    *,
    gate_registry: Mapping[str, gates.GateProfile] | None = None,
) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("source manifest must be an object")
    unknown = set(raw) - MANIFEST_KEYS
    missing = MANIFEST_KEYS - set(raw)
    if unknown:
        raise ValueError(f"unknown source manifest keys: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing source manifest keys: {sorted(missing)}")

    source_id = raw["source_id"]
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError("source_id must be a simple stable identifier")
    if raw["adapter"] != "git_source":
        raise ValueError("V0.3 source adapter must be 'git_source'")
    repository = _require_git_repo(raw["repository"])
    if not isinstance(raw["baseline_ref"], str) or not raw["baseline_ref"].strip():
        raise ValueError("baseline_ref must be non-empty text")
    baseline_sha = _resolve_commit(repository, raw["baseline_sha"], label="baseline_sha")

    write_policy = raw["write_policy"]
    if not isinstance(write_policy, dict) or set(write_policy) != WRITE_POLICY_KEYS:
        raise ValueError("write_policy must contain exactly allow and deny")
    allow = _validate_patterns(
        write_policy["allow"], label="write_policy.allow", require_nonempty=True
    )
    deny = _validate_patterns(
        write_policy["deny"], label="write_policy.deny", require_nonempty=False
    )

    patch_policy = raw["patch_policy"]
    if not isinstance(patch_policy, dict) or set(patch_policy) != PATCH_POLICY_KEYS:
        raise ValueError(
            "patch_policy must contain exactly max_files, max_total_lines, text_only"
        )
    max_files = patch_policy["max_files"]
    max_lines = patch_policy["max_total_lines"]
    for key, value in (("max_files", max_files), ("max_total_lines", max_lines)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"patch_policy.{key} must be a positive integer")
    if max_files > 100 or max_lines > 100000:
        raise ValueError("patch_policy limits exceed V0.3 safety bounds")
    if patch_policy["text_only"] is not True:
        raise ValueError("V0.3 patch_policy.text_only must be true")

    gate_cfg = raw["gates"]
    if not isinstance(gate_cfg, dict) or set(gate_cfg) != GATE_KEYS:
        raise ValueError(
            "gates must contain exactly static, build, regression, performance"
        )
    normalized_gates = {}
    for name in ("static", "build", "regression"):
        profile_id = gate_cfg[name]
        if not isinstance(profile_id, str) or not gates.has_profile(profile_id, gate_registry):
            raise ValueError(f"unknown registered gate profile {profile_id!r}")
        normalized_gates[name] = profile_id
    performance = gate_cfg["performance"]
    if performance is not None:
        if not isinstance(performance, str) or not gates.has_profile(performance, gate_registry):
            raise ValueError(f"unknown registered gate profile {performance!r}")
    normalized_gates["performance"] = performance

    return {
        "source_id": source_id,
        "adapter": "git_source",
        "repository": repository,
        "baseline_ref": raw["baseline_ref"].strip(),
        "baseline_sha": baseline_sha,
        "write_policy": {"allow": allow, "deny": deny},
        "patch_policy": {
            "max_files": max_files,
            "max_total_lines": max_lines,
            "text_only": True,
        },
        "gates": normalized_gates,
    }


def _read_registry(ws: str) -> dict:
    path = _registry_path(ws)
    if not os.path.exists(path):
        return {"version": 1, "sources": {}}
    value = _load_json(path)
    if value.get("version") != 1 or not isinstance(value.get("sources"), dict):
        raise ValueError("invalid source registry")
    return value


def accepted_ref(source_id: str) -> str:
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError("invalid source_id for Git ref")
    return f"refs/wikiskill/{source_id}/accepted"


def _read_ref(repo: str, ref: str) -> str | None:
    p = _git(repo, "rev-parse", "--verify", ref)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def _state_value(
    source_id: str,
    accepted: str,
    *,
    previous: str | None = None,
    iteration: int | None = None,
) -> dict:
    return {
        "source_id": source_id,
        "accepted_sha": accepted,
        "previous_sha": previous,
        "accepted_iteration": iteration,
        "updated_at": _now(),
    }


def register_source(
    ws: str,
    manifest_path: str,
    *,
    gate_registry: Mapping[str, gates.GateProfile] | None = None,
) -> dict:
    raw = _load_json(manifest_path)
    manifest = normalize_manifest(raw, gate_registry=gate_registry)
    _ensure_dirs(ws)
    registry = _read_registry(ws)
    source_id = manifest["source_id"]
    existing_entry = registry["sources"].get(source_id)
    if existing_entry:
        existing = get_manifest(ws, source_id)
        if existing["repository"] != manifest["repository"]:
            raise ValueError("re-registration cannot change repository identity")
        if existing["baseline_sha"] != manifest["baseline_sha"]:
            raise ValueError("re-registration cannot change original baseline anchor")

    ref = accepted_ref(source_id)
    current = _read_ref(manifest["repository"], ref)
    if current is None:
        p = _git(manifest["repository"], "update-ref", ref, manifest["baseline_sha"], "")
        if p.returncode != 0:
            raise SourceOperationalError(
                f"could not create accepted source ref {ref}: {p.stderr.strip()}"
            )
        current = manifest["baseline_sha"]

    _atomic_json(_manifest_path(ws, source_id), manifest)
    registry["sources"][source_id] = f"manifests/{source_id}.json"
    _atomic_json(_registry_path(ws), registry)
    _repair_state(ws, source_id, current)
    return manifest


def get_manifest(ws: str, source_id: str) -> dict:
    registry = _read_registry(ws)
    if source_id not in registry["sources"]:
        raise ValueError(f"unknown source_id {source_id!r}")
    manifest = _load_json(_manifest_path(ws, source_id))
    if manifest.get("source_id") != source_id:
        raise ValueError(f"source manifest id mismatch for {source_id!r}")
    return manifest


def _load_state_if_valid(ws: str, source_id: str) -> dict | None:
    path = _state_path(ws, source_id)
    if not os.path.exists(path):
        return None
    try:
        value = _load_json(path)
    except ValueError:
        return None
    if value.get("source_id") != source_id:
        return None
    return value


def _repair_state(ws: str, source_id: str, authoritative_sha: str) -> dict:
    old = _load_state_if_valid(ws, source_id)
    if old and old.get("accepted_sha") == authoritative_sha:
        return old
    previous = old.get("accepted_sha") if old else None
    iteration = old.get("accepted_iteration") if old else None
    value = _state_value(
        source_id, authoritative_sha, previous=previous, iteration=iteration
    )
    try:
        _atomic_json(_state_path(ws, source_id), value)
    except OSError as exc:
        raise SourceOperationalError(f"could not repair source state: {exc}") from exc
    return value


def accepted_sha(ws: str, source_id: str) -> str:
    manifest = get_manifest(ws, source_id)
    ref = accepted_ref(source_id)
    sha = _read_ref(manifest["repository"], ref)
    if sha is None:
        raise SourceOperationalError(f"accepted source ref is missing: {ref}")
    _resolve_commit(manifest["repository"], sha, label="accepted_sha")
    _repair_state(ws, source_id, sha)
    return sha


def advance_accepted_sha(
    ws: str,
    source_id: str,
    expected_old: str,
    new_sha: str,
    iteration: int,
) -> dict:
    manifest = get_manifest(ws, source_id)
    repo = manifest["repository"]
    new_sha = _resolve_commit(repo, new_sha, label="new accepted SHA")
    expected_old = _resolve_commit(repo, expected_old, label="expected accepted SHA")
    ref = accepted_ref(source_id)
    p = _git(repo, "update-ref", ref, new_sha, expected_old)
    if p.returncode != 0:
        raise SourceOperationalError(
            f"accepted source ref compare-and-swap failed for {source_id}: {p.stderr.strip()}"
        )

    value = _state_value(
        source_id, new_sha, previous=expected_old, iteration=iteration
    )
    try:
        _atomic_json(_state_path(ws, source_id), value)
    except OSError as exc:
        revert = _git(repo, "update-ref", ref, expected_old, new_sha)
        detail = ""
        if revert.returncode != 0:
            detail = f"; compensating ref rollback also failed: {revert.stderr.strip()}"
        raise SourceOperationalError(
            f"accepted source state write failed: {exc}{detail}"
        ) from exc
    return value


def inspect_source(ws: str, source_id: str) -> dict:
    manifest = get_manifest(ws, source_id)
    sha = accepted_sha(ws, source_id)
    state = _load_state_if_valid(ws, source_id)
    if state is None or state.get("accepted_sha") != sha:
        state = _repair_state(ws, source_id, sha)
    return {"manifest": manifest, "state": state}


def list_sources(ws: str) -> list[dict]:
    registry = _read_registry(ws)
    out = []
    for source_id in sorted(registry["sources"]):
        item = inspect_source(ws, source_id)
        manifest = item["manifest"]
        out.append(
            {
                "source_id": source_id,
                "adapter": manifest["adapter"],
                "accepted_sha": item["state"]["accepted_sha"],
                "gates": dict(manifest["gates"]),
            }
        )
    return out


def validate_registered_source(
    ws: str,
    source_id: str,
    *,
    gate_registry: Mapping[str, gates.GateProfile] | None = None,
) -> dict:
    manifest = get_manifest(ws, source_id)
    normalized = normalize_manifest(manifest, gate_registry=gate_registry)
    sha = accepted_sha(ws, source_id)
    _resolve_commit(normalized["repository"], sha, label="accepted_sha")
    return {
        "status": "valid",
        "source_id": source_id,
        "accepted_sha": sha,
        "adapter": normalized["adapter"],
        "gates": dict(normalized["gates"]),
    }


def proposer_source_summary(ws: str) -> str:
    rows = []
    for item in list_sources(ws):
        rows.append(
            f"- source_id: {item['source_id']} | accepted_sha={item['accepted_sha']} "
            f"| gates={json.dumps(item['gates'], sort_keys=True)}"
        )
    return "\n".join(rows) if rows else "- no registered sources"
