"""Governed Git source mutation for WikiSkill-Evolution V0.3.

Core proposals are data, never commands. This module validates a proposal
against a trusted source manifest, plans text edits before creating a worktree,
runs trusted engineering gates, and advances the accepted Git ref only after a
candidate is safely committed and its worktree is removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import fnmatch
import os
import subprocess
from typing import Any, Mapping

from . import gates, sources


class CoreOperationalError(RuntimeError):
    """The source adapter could not complete an infrastructure operation safely."""


@dataclass
class CandidateSession:
    ws: str
    iteration: int
    proposal: dict
    source_id: str
    manifest: dict
    repo: str
    base_sha: str
    worktree: str
    changed_files: tuple[str, ...]
    base_contents: dict[str, str]
    planned_contents: dict[str, str]
    gate_results: list[dict] = field(default_factory=list)
    candidate_sha: str | None = None
    cleanup: dict | None = None
    applied: bool = False


TOP_KEYS = {"target", "action", "source_id", "base_sha", "edits"}
EDIT_KEYS = {
    "append": {"file", "op", "content"},
    "replace": {"file", "op", "target", "content"},
    "insert_after": {"file", "op", "target", "content"},
}


def _git(repo: str, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=text,
        check=False,
    )


def _safe_relpath(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("core edit file must be a non-empty relative path")
    if "\\" in value or value.startswith("/") or os.path.isabs(value):
        raise ValueError(f"unsafe core source path {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe core source path {value!r}")
    normalized = os.path.normpath(value).replace(os.sep, "/")
    if normalized != value or normalized.startswith("../"):
        raise ValueError(f"unsafe core source path {value!r}")
    return value


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _validate_policy_path(path: str, manifest: dict) -> None:
    policy = manifest["write_policy"]
    if not _matches(path, policy["allow"]):
        raise ValueError(f"core source path is not allowlisted: {path}")
    if _matches(path, policy["deny"]):
        raise ValueError(f"core source path is denied: {path}")


def _tree_entry(repo: str, sha: str, path: str) -> tuple[str, str] | None:
    p = _git(repo, "ls-tree", sha, "--", path)
    if p.returncode != 0:
        raise CoreOperationalError(f"git ls-tree failed for {path}: {p.stderr.strip()}")
    rows = [line for line in p.stdout.splitlines() if line.strip()]
    exact = []
    for row in rows:
        try:
            left, shown = row.split("\t", 1)
            mode, kind, _oid = left.split(" ", 2)
        except ValueError:
            continue
        if shown == path:
            exact.append((mode, kind))
    if not exact:
        return None
    if len(exact) != 1:
        raise ValueError(f"ambiguous Git tree entry for {path}")
    return exact[0]


def _validate_tree_path(repo: str, sha: str, path: str) -> None:
    parts = path.split("/")
    for end in range(1, len(parts)):
        ancestor = "/".join(parts[:end])
        entry = _tree_entry(repo, sha, ancestor)
        if entry and entry[0] == "160000":
            raise ValueError(f"core source path crosses submodule boundary: {path}")
    entry = _tree_entry(repo, sha, path)
    if entry is None:
        raise ValueError(f"core source file is not tracked at accepted SHA: {path}")
    mode, kind = entry
    if mode == "120000":
        raise ValueError(f"core source symlink mode is not patchable: {path}")
    if mode == "160000" or kind == "commit":
        raise ValueError(f"core source submodule mode is not patchable: {path}")
    if mode not in ("100644", "100755") or kind != "blob":
        raise ValueError(f"unsupported core source Git mode {mode} for {path}")


def _read_base_text(repo: str, sha: str, path: str) -> str:
    p = _git(repo, "show", f"{sha}:{path}", text=False)
    if p.returncode != 0:
        stderr = (p.stderr or b"").decode("utf-8", "replace")
        raise ValueError(f"cannot read core source file {path}: {stderr.strip()}")
    data = p.stdout or b""
    if b"\x00" in data:
        raise ValueError(f"binary core source file is not patchable text: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"core source file must be UTF-8 text: {path}") from exc


def _apply_edit(content: str, edit: dict, path: str) -> str:
    op = edit["op"]
    body = edit["content"]
    if not isinstance(body, str):
        raise ValueError(f"core edit content must be text: {path}")
    if op == "append":
        return content + "\n" + body
    target = edit["target"]
    if not isinstance(target, str) or not target:
        raise ValueError(f"core {op} requires non-empty target text: {path}")
    count = content.count(target)
    if count != 1:
        raise ValueError(
            f"core {op} target must occur exactly once in {path}; found {count}"
        )
    if op == "replace":
        return content.replace(target, body, 1)
    return content.replace(target, target + "\n" + body, 1)


def _changed_line_count(before: str, after: str, path: str) -> int:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    total = 0
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            total += 1
    return total


def _plan(ws: str, proposal: dict) -> tuple[dict, dict[str, str], dict[str, str]]:
    if not isinstance(proposal, dict):
        raise ValueError("core proposal must be an object")
    unknown = set(proposal) - TOP_KEYS
    missing = TOP_KEYS - set(proposal)
    if unknown:
        raise ValueError(f"unknown core proposal keys: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing core proposal keys: {sorted(missing)}")
    if proposal["target"] != "core" or proposal["action"] != "patch":
        raise ValueError("V0.3 core proposal must use target='core' and action='patch'")
    source_id = proposal["source_id"]
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("core proposal source_id must be non-empty text")
    manifest = sources.get_manifest(ws, source_id)
    accepted = sources.accepted_sha(ws, source_id)
    if proposal["base_sha"] != accepted:
        raise ValueError(
            f"stale core base_sha: proposal={proposal['base_sha']} accepted={accepted}"
        )
    edits = proposal["edits"]
    if not isinstance(edits, list) or not edits:
        raise ValueError("core proposal edits must be a non-empty list")

    grouped: dict[str, list[dict]] = {}
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("core edit must be an object")
        op = edit.get("op")
        if op not in EDIT_KEYS:
            raise ValueError(f"unknown core edit op {op!r}")
        unknown_edit = set(edit) - EDIT_KEYS[op]
        missing_edit = EDIT_KEYS[op] - set(edit)
        if unknown_edit or missing_edit:
            raise ValueError(
                f"invalid core {op} edit keys; unknown={sorted(unknown_edit)} "
                f"missing={sorted(missing_edit)}"
            )
        path = _safe_relpath(edit["file"])
        _validate_policy_path(path, manifest)
        grouped.setdefault(path, []).append(dict(edit, file=path))

    max_files = manifest["patch_policy"]["max_files"]
    if len(grouped) > max_files:
        raise ValueError(
            f"core patch exceeds max_files={max_files}: {len(grouped)} files"
        )

    repo = manifest["repository"]
    base_contents: dict[str, str] = {}
    planned: dict[str, str] = {}
    total_lines = 0
    for path, file_edits in grouped.items():
        _validate_tree_path(repo, accepted, path)
        before = _read_base_text(repo, accepted, path)
        after = before
        for edit in file_edits:
            after = _apply_edit(after, edit, path)
        if after == before:
            raise ValueError(f"core patch makes no change to {path}")
        base_contents[path] = before
        planned[path] = after
        total_lines += _changed_line_count(before, after, path)

    max_lines = manifest["patch_policy"]["max_total_lines"]
    if total_lines > max_lines:
        raise ValueError(
            f"core patch changed-line budget {total_lines} exceeds max_total_lines={max_lines}"
        )
    normalized = {
        "target": "core",
        "action": "patch",
        "source_id": source_id,
        "base_sha": accepted,
        "edits": [dict(edit) for edit in edits],
    }
    return normalized, base_contents, planned


def validate_core_proposal(ws: str, proposal: dict) -> dict:
    normalized, _base, _planned = _plan(ws, proposal)
    return normalized


def _candidate_root(ws: str) -> str:
    return os.path.join(ws, "runs", "core-worktrees")


def begin_candidate(ws: str, iteration: int, proposal: dict) -> CandidateSession:
    normalized, base_contents, planned = _plan(ws, proposal)
    manifest = sources.get_manifest(ws, normalized["source_id"])
    base_sha = normalized["base_sha"]
    root = _candidate_root(ws)
    name = f"iter-{iteration:02d}-{normalized['source_id']}-{base_sha[:12]}"
    worktree = os.path.realpath(os.path.join(root, name))
    root_real = os.path.realpath(root)
    if os.path.commonpath([root_real, worktree]) != root_real:
        raise CoreOperationalError("generated core worktree escaped workspace root")
    if os.path.exists(worktree):
        raise CoreOperationalError(f"core candidate worktree already exists: {worktree}")
    os.makedirs(root_real, exist_ok=True)
    p = _git(
        manifest["repository"],
        "worktree",
        "add",
        "--detach",
        worktree,
        base_sha,
    )
    if p.returncode != 0:
        raise CoreOperationalError(f"could not create core candidate worktree: {p.stderr.strip()}")
    return CandidateSession(
        ws=ws,
        iteration=iteration,
        proposal=normalized,
        source_id=normalized["source_id"],
        manifest=manifest,
        repo=manifest["repository"],
        base_sha=base_sha,
        worktree=worktree,
        changed_files=tuple(sorted(planned)),
        base_contents=base_contents,
        planned_contents=planned,
    )


def _all_tracked_changes(session: CandidateSession) -> tuple[str, ...]:
    p = _git(session.worktree, "diff", "--name-only", "HEAD", "--")
    if p.returncode != 0:
        raise CoreOperationalError(f"could not inspect candidate diff: {p.stderr.strip()}")
    return tuple(sorted(line.strip() for line in p.stdout.splitlines() if line.strip()))


def _verify_integrity(session: CandidateSession) -> None:
    actual = _all_tracked_changes(session)
    if actual != session.changed_files:
        raise ValueError(
            f"core candidate tracked diff scope changed: expected={session.changed_files} actual={actual}"
        )
    total_lines = 0
    for path in session.changed_files:
        _validate_policy_path(path, session.manifest)
        full = os.path.realpath(os.path.join(session.worktree, *path.split("/")))
        if os.path.commonpath([os.path.realpath(session.worktree), full]) != os.path.realpath(session.worktree):
            raise ValueError(f"candidate file escaped worktree: {path}")
        if os.path.islink(full) or not os.path.isfile(full):
            raise ValueError(f"candidate file is no longer a regular file: {path}")
        try:
            data = open(full, "rb").read()
        except OSError as exc:
            raise CoreOperationalError(f"cannot read candidate file {path}: {exc}") from exc
        if b"\x00" in data:
            raise ValueError(f"candidate file became binary: {path}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"candidate file is no longer UTF-8 text: {path}") from exc
        if text != session.planned_contents[path]:
            raise ValueError(f"trusted gate modified candidate source content: {path}")
        total_lines += _changed_line_count(session.base_contents[path], text, path)
    limit = session.manifest["patch_policy"]["max_total_lines"]
    if total_lines > limit:
        raise ValueError(
            f"candidate changed-line budget {total_lines} exceeds max_total_lines={limit}"
        )


def apply_candidate(session: CandidateSession) -> str:
    if session.applied:
        raise CoreOperationalError("core candidate has already been applied")
    try:
        for path in session.changed_files:
            full = os.path.join(session.worktree, *path.split("/"))
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(session.planned_contents[path])
    except OSError as exc:
        raise CoreOperationalError(f"could not write core candidate: {exc}") from exc
    session.applied = True
    _verify_integrity(session)
    return f"patch core source:{session.source_id} files={len(session.changed_files)}"


def candidate_diff(session: CandidateSession) -> str:
    if not session.applied:
        return ""
    p = _git(
        session.worktree,
        "diff",
        "--no-ext-diff",
        "HEAD",
        "--",
        *session.changed_files,
    )
    if p.returncode != 0:
        raise CoreOperationalError(f"could not render core candidate diff: {p.stderr.strip()}")
    return p.stdout


def _not_configured_performance() -> dict:
    return {
        "gate": "performance",
        "profile": None,
        "status": "not_configured",
        "exit_code": None,
        "duration_s": 0.0,
        "summary": "performance gate is explicitly not configured for this source",
        "evidence": {"artifact_refs": [], "metrics": {}},
    }


def run_pre_gates(
    session: CandidateSession,
    *,
    gate_registry: Mapping[str, gates.GateProfile] | None = None,
) -> list[dict]:
    if not session.applied:
        raise CoreOperationalError("core candidate must be applied before engineering gates")
    results: list[dict] = []
    config = session.manifest["gates"]
    for gate_name in ("static", "build", "regression"):
        try:
            result = gates.run_profile(
                gate_name,
                config[gate_name],
                session.worktree,
                gate_registry,
            )
        except gates.GateOperationalError as exc:
            raise CoreOperationalError(str(exc)) from exc
        results.append(result)
        session.gate_results = list(results)
        if result["status"] != "pass":
            return results
        _verify_integrity(session)

    performance_id = config["performance"]
    if performance_id is None:
        results.append(_not_configured_performance())
    else:
        try:
            results.append(
                gates.run_profile(
                    "performance", performance_id, session.worktree, gate_registry
                )
            )
        except gates.GateOperationalError as exc:
            raise CoreOperationalError(str(exc)) from exc
        if results[-1]["status"] == "pass":
            _verify_integrity(session)
    session.gate_results = list(results)
    return results


def _cleanup_worktree(session: CandidateSession) -> dict:
    if not os.path.exists(session.worktree):
        value = {"removed": True, "worktree": session.worktree, "already_missing": True}
        session.cleanup = value
        return value
    p = _git(session.repo, "worktree", "remove", "--force", session.worktree)
    if p.returncode != 0:
        raise CoreOperationalError(f"could not remove core candidate worktree: {p.stderr.strip()}")
    prune = _git(session.repo, "worktree", "prune")
    if prune.returncode != 0:
        raise CoreOperationalError(f"could not prune core candidate worktrees: {prune.stderr.strip()}")
    value = {"removed": not os.path.exists(session.worktree), "worktree": session.worktree}
    session.cleanup = value
    if not value["removed"]:
        raise CoreOperationalError("core candidate worktree remained after cleanup")
    return value


def reject_candidate(session: CandidateSession) -> dict:
    cleanup = _cleanup_worktree(session)
    return {
        "source_id": session.source_id,
        "base_sha": session.base_sha,
        "candidate_sha": session.candidate_sha,
        "cleanup": cleanup,
        "accepted_sha": sources.accepted_sha(session.ws, session.source_id),
    }


def _stage_expected_files(session: CandidateSession) -> None:
    p = _git(session.worktree, "add", "--", *session.changed_files)
    if p.returncode != 0:
        raise CoreOperationalError(f"could not stage core candidate files: {p.stderr.strip()}")
    staged = _git(session.worktree, "diff", "--cached", "--name-only", "HEAD", "--")
    if staged.returncode != 0:
        raise CoreOperationalError(f"could not inspect staged core candidate: {staged.stderr.strip()}")
    actual = tuple(sorted(line.strip() for line in staged.stdout.splitlines() if line.strip()))
    if actual != session.changed_files:
        raise ValueError(
            f"staged core candidate scope changed: expected={session.changed_files} actual={actual}"
        )


def accept_candidate(session: CandidateSession, iteration: int) -> dict:
    if not session.applied:
        raise CoreOperationalError("cannot accept an unapplied core candidate")
    _verify_integrity(session)
    _stage_expected_files(session)
    p = _git(
        session.worktree,
        "-c",
        "user.email=wikiskill@local",
        "-c",
        "user.name=wikiskill-evolution",
        "commit",
        "-q",
        "-m",
        f"wikiskill core candidate iter-{iteration}",
    )
    if p.returncode != 0:
        raise CoreOperationalError(f"could not commit core candidate: {p.stderr.strip()}")
    head = _git(session.worktree, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise CoreOperationalError(f"could not resolve core candidate commit: {head.stderr.strip()}")
    candidate_sha = head.stdout.strip()
    session.candidate_sha = candidate_sha

    temporary_ref = f"refs/wikiskill/{session.source_id}/candidate-{iteration}"
    temp = _git(session.repo, "update-ref", temporary_ref, candidate_sha, "")
    if temp.returncode != 0:
        raise CoreOperationalError(
            f"could not create temporary candidate ref: {temp.stderr.strip()}"
        )

    cleanup = _cleanup_worktree(session)
    try:
        state = sources.advance_accepted_sha(
            session.ws,
            session.source_id,
            session.base_sha,
            candidate_sha,
            iteration,
        )
    except sources.SourceOperationalError as exc:
        raise CoreOperationalError(str(exc)) from exc

    delete = _git(session.repo, "update-ref", "-d", temporary_ref, candidate_sha)
    temp_ref_removed = delete.returncode == 0
    return {
        "source_id": session.source_id,
        "base_sha": session.base_sha,
        "candidate_sha": candidate_sha,
        "accepted_sha": state["accepted_sha"],
        "cleanup": cleanup,
        "temporary_ref_removed": temp_ref_removed,
    }


def audit_evidence(session: CandidateSession) -> dict:
    return {
        "source_id": session.source_id,
        "base_sha": session.base_sha,
        "candidate_sha": session.candidate_sha,
        "changed_files": list(session.changed_files),
        "gates": list(session.gate_results),
        "cleanup": session.cleanup,
    }
