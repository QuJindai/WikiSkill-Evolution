"""Governed candidate assets for WikiSkill-Evolution V0.2.

Each asset driver owns mutation and rollback for one candidate type. Scoring
and acceptance remain in the harness/gating layer; drivers never decide if a
candidate is good.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from . import core_adapter

TARGETS = ("skill", "prompt", "harness", "core")

HARNESS_POLICY_SCHEMA = {
    "inference_max_turns": (1, 500),
    "maintainer_max_turns": (1, 500),
    "proposer_max_turns": (1, 500),
    "maintainer_run_budget": (1, 100000),
    "proposer_run_budget": (1, 100000),
}


def normalize_proposal(proposal: dict) -> dict:
    """Return a normalized proposal, defaulting legacy proposals to skill."""
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a JSON object")
    out = dict(proposal)
    out.setdefault("target", "skill")
    action = out.get("action")
    if action not in ("create", "patch", "no_action"):
        raise ValueError(f"unknown proposal action {action!r}")
    if out["target"] not in TARGETS:
        raise ValueError(f"unknown asset target {out['target']!r}")
    return out


def safe_asset_path(root: str, relative: str) -> str:
    """Resolve a relative path inside root and reject traversal/symlink escape."""
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        raise ValueError(f"unsafe asset path {relative!r}")
    normalized = os.path.normpath(relative)
    if normalized in (".", "..") or normalized.startswith(".." + os.sep):
        raise ValueError(f"unsafe asset path {relative!r}")
    root_real = os.path.realpath(os.path.abspath(root))
    candidate = os.path.abspath(os.path.join(root_real, normalized))
    candidate_real = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([root_real, candidate_real])
    except ValueError:
        raise ValueError(f"unsafe asset path {relative!r}") from None
    if common != root_real:
        raise ValueError(f"unsafe asset path {relative!r}")
    return candidate


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("candidate name must be a simple non-empty name")
    if os.path.basename(name) != name or "/" in name or "\\" in name:
        raise ValueError(f"unsafe candidate name {name!r}")
    return name


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=False)


def _git_commit(root: str, message: str, *, allow_empty: bool = False) -> None:
    _git(root, "add", "-A")
    cmd = ["-c", "user.email=wikiskill@local", "-c", "user.name=wikiskill",
           "commit", "-q"]
    if allow_empty:
        cmd.append("--allow-empty")
    cmd += ["-m", message]
    p = _git(root, *cmd)
    if p.returncode != 0 and "nothing to commit" not in (p.stdout + p.stderr).lower():
        raise RuntimeError(f"git commit failed in {root}: {p.stderr.strip()}")


def _ensure_git_repo(root: str, initial_message: str) -> None:
    os.makedirs(root, exist_ok=True)
    if not os.path.isdir(os.path.join(root, ".git")):
        p = subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True,
                           text=True, check=False)
        if p.returncode != 0:
            raise RuntimeError(f"git init failed in {root}: {p.stderr.strip()}")
        _git_commit(root, initial_message, allow_empty=True)


def _working_diff(root: str) -> str:
    """Return a unified diff including newly-created files without keeping them staged."""
    _git(root, "add", "-A")
    p = _git(root, "diff", "--cached", "--no-ext-diff")
    _git(root, "reset", "-q")
    return p.stdout


def _apply_text_edits(content: str, edits: Any, *, label: str) -> str:
    if not isinstance(edits, list) or not edits:
        raise ValueError(f"{label} patch requires non-empty edits")
    out = content
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError(f"{label} edit must be an object")
        op = edit.get("op")
        body = edit.get("content")
        if op not in ("append", "replace", "insert_after"):
            raise ValueError(f"unknown patch op {op!r}")
        if not isinstance(body, str):
            raise ValueError(f"{label} edit content must be text")
        if op == "append":
            out += "\n" + body
            continue
        target = edit.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError(f"{label} {op} requires target text")
        if target not in out:
            raise ValueError(f"patch {op} target not found in {label}: {target[:60]!r}")
        if op == "replace":
            out = out.replace(target, body, 1)
        else:
            out = out.replace(target, target + "\n" + body, 1)
    return out


class GitAssetDriver:
    target = ""

    def root(self, ws: str) -> str:
        raise NotImplementedError

    def initial_message(self) -> str:
        return f"A0: empty {self.target} asset set"

    def prepare(self, ws: str, iteration: int, proposal: dict | None = None):
        root = self.root(ws)
        _ensure_git_repo(root, self.initial_message())
        _git_commit(root, f"base iter-{iteration}", allow_empty=True)
        return None

    def validate(self, ws: str, proposal: dict) -> None:
        raise NotImplementedError

    def apply(self, ws: str, proposal: dict, context=None) -> str:
        raise NotImplementedError

    def diff(self, ws: str, context=None) -> str:
        return _working_diff(self.root(ws))

    def pre_gates(self, ws: str, proposal: dict, context=None) -> list[dict]:
        return []

    def accept(self, ws: str, iteration: int, score: float, context=None):
        _git_commit(self.root(ws), f"accept iter-{iteration} R={score}", allow_empty=True)
        return None

    def rollback(self, ws: str, context=None):
        root = self.root(ws)
        _git(root, "reset", "--hard", "-q")
        _git(root, "clean", "-fd", "-q")
        return None


class SkillDriver(GitAssetDriver):
    target = "skill"

    def root(self, ws: str) -> str:
        return os.path.join(ws, "skills", "active")

    def initial_message(self) -> str:
        return "S0: empty skill set"

    def validate(self, ws: str, proposal: dict) -> None:
        action = proposal.get("action")
        if action not in ("create", "patch"):
            raise ValueError(f"skill driver does not support action {action!r}")
        name = _validate_name(proposal.get("name"))
        dest = safe_asset_path(self.root(ws), name)
        if action == "create":
            if not isinstance(proposal.get("skill_md"), str):
                raise ValueError("skill create requires skill_md text")
            purpose = proposal.get("purpose_md")
            if purpose is not None and not isinstance(purpose, str):
                raise ValueError("purpose_md must be text")
        else:
            target_file = proposal.get("file", "SKILL.md")
            path = safe_asset_path(dest, target_file)
            if not os.path.isfile(path):
                raise ValueError(f"skill patch target does not exist: {target_file}")
            _apply_text_edits(open(path, encoding="utf-8").read(), proposal.get("edits"),
                              label=name)

    def apply(self, ws: str, proposal: dict, context=None) -> str:
        self.validate(ws, proposal)
        action = proposal["action"]
        name = proposal["name"]
        dest = safe_asset_path(self.root(ws), name)
        os.makedirs(dest, exist_ok=True)
        if action == "create":
            with open(safe_asset_path(dest, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(proposal["skill_md"])
            if proposal.get("purpose_md") is not None:
                if not isinstance(proposal["purpose_md"], str):
                    raise ValueError("purpose_md must be text")
                with open(safe_asset_path(dest, "PURPOSE.md"), "w", encoding="utf-8") as f:
                    f.write(proposal["purpose_md"])
        else:
            target_file = proposal.get("file", "SKILL.md")
            path = safe_asset_path(dest, target_file)
            content = open(path, encoding="utf-8").read()
            content = _apply_text_edits(content, proposal["edits"], label=name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return f"{action} {name}"


class PromptDriver(GitAssetDriver):
    target = "prompt"

    def root(self, ws: str) -> str:
        return os.path.join(ws, "assets", "prompts", "active")

    def validate(self, ws: str, proposal: dict) -> None:
        action = proposal.get("action")
        if action not in ("create", "patch"):
            raise ValueError(f"prompt driver does not support action {action!r}")
        if proposal.get("name") != "inference":
            raise ValueError("V0.2 prompt driver only supports name='inference'")
        path = prompt_overlay_path(ws)
        if action == "create":
            if not isinstance(proposal.get("content"), str):
                raise ValueError("prompt create requires content text")
        else:
            if not os.path.isfile(path):
                raise ValueError("prompt patch requires an existing inference overlay")
            _apply_text_edits(open(path, encoding="utf-8").read(), proposal.get("edits"),
                              label="inference prompt")

    def apply(self, ws: str, proposal: dict, context=None) -> str:
        self.validate(ws, proposal)
        path = prompt_overlay_path(ws)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if proposal["action"] == "create":
            content = proposal["content"]
        else:
            content = _apply_text_edits(open(path, encoding="utf-8").read(),
                                        proposal["edits"], label="inference prompt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"{proposal['action']} prompt:{proposal['name']}"


class HarnessDriver(GitAssetDriver):
    target = "harness"

    def root(self, ws: str) -> str:
        return os.path.join(ws, "assets", "harness", "active")

    def _resulting_policy(self, ws: str, proposal: dict) -> dict:
        action = proposal.get("action")
        if action not in ("create", "patch"):
            raise ValueError(f"harness driver does not support action {action!r}")
        if proposal.get("name") != "policy":
            raise ValueError("harness candidate name must be 'policy'")
        if action == "create":
            raw = proposal.get("policy")
            if not isinstance(raw, dict):
                raise ValueError("harness create requires policy object")
            policy = dict(raw)
        else:
            updates = proposal.get("updates")
            if not isinstance(updates, dict) or not updates:
                raise ValueError("harness patch requires non-empty updates object")
            policy = read_harness_policy(ws)
            policy.update(updates)
        validate_harness_policy(policy)
        return policy

    def validate(self, ws: str, proposal: dict) -> None:
        self._resulting_policy(ws, proposal)

    def apply(self, ws: str, proposal: dict, context=None) -> str:
        policy = self._resulting_policy(ws, proposal)
        path = harness_policy_path(ws)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(policy, f, indent=2, sort_keys=True)
            f.write("\n")
        return f"{proposal['action']} harness:policy"


class CoreDriver(GitAssetDriver):
    target = "core"

    def root(self, ws: str) -> str:
        return os.path.join(ws, "assets", "core", "active")

    def validate(self, ws: str, proposal: dict) -> None:
        core_adapter.validate_core_proposal(ws, proposal)

    def prepare(self, ws: str, iteration: int, proposal: dict | None = None):
        if proposal is None:
            raise ValueError("core prepare requires proposal")
        return core_adapter.begin_candidate(ws, iteration, proposal)

    def apply(self, ws: str, proposal: dict, context=None) -> str:
        if context is None:
            raise ValueError("core apply requires candidate context")
        return core_adapter.apply_candidate(context)

    def diff(self, ws: str, context=None) -> str:
        if context is None:
            raise ValueError("core diff requires candidate context")
        return core_adapter.candidate_diff(context)

    def pre_gates(self, ws: str, proposal: dict, context=None) -> list[dict]:
        if context is None:
            raise ValueError("core pre_gates requires candidate context")
        return core_adapter.run_pre_gates(context)

    def accept(self, ws: str, iteration: int, score: float, context=None):
        if context is None:
            raise ValueError("core accept requires candidate context")
        return core_adapter.accept_candidate(context, iteration)

    def rollback(self, ws: str, context=None):
        if context is None:
            raise ValueError("core rollback requires candidate context")
        return core_adapter.reject_candidate(context)


_SKILL_DRIVER = SkillDriver()
_PROMPT_DRIVER = PromptDriver()
_HARNESS_DRIVER = HarnessDriver()
_CORE_DRIVER = CoreDriver()
_DRIVERS = {
    "skill": _SKILL_DRIVER,
    "prompt": _PROMPT_DRIVER,
    "harness": _HARNESS_DRIVER,
    "core": _CORE_DRIVER,
}


def resolve_driver(target: str):
    try:
        return _DRIVERS[target]
    except KeyError:
        raise ValueError(f"unknown asset target {target!r}") from None


def prompt_overlay_path(ws: str, name: str = "inference") -> str:
    if name != "inference":
        raise ValueError("V0.2 prompt driver only supports inference overlay")
    return safe_asset_path(_PROMPT_DRIVER.root(ws), f"{name}.txt")


def read_prompt_overlay(ws: str | None, name: str = "inference") -> str:
    if not ws:
        return ""
    path = prompt_overlay_path(ws, name)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def harness_policy_path(ws: str) -> str:
    return safe_asset_path(_HARNESS_DRIVER.root(ws), "policy.json")


def validate_harness_policy(policy: dict) -> None:
    if not isinstance(policy, dict):
        raise ValueError("harness policy must be an object")
    for key, value in policy.items():
        if key not in HARNESS_POLICY_SCHEMA:
            raise ValueError(f"unknown harness policy key {key!r}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"harness policy {key} must be an integer")
        lo, hi = HARNESS_POLICY_SCHEMA[key]
        if not lo <= value <= hi:
            raise ValueError(f"harness policy {key} must be in range {lo}..{hi}")


def read_harness_policy(ws: str | None) -> dict:
    if not ws:
        return {}
    path = harness_policy_path(ws)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid harness policy file: {exc}") from exc
    validate_harness_policy(value)
    return value


def resolve_harness_value(ws: str | None, key: str, default: int) -> int:
    if key not in HARNESS_POLICY_SCHEMA:
        raise ValueError(f"unknown harness policy key {key!r}")
    return read_harness_policy(ws).get(key, default)


def core_manifest_path(ws: str) -> str:
    return safe_asset_path(_CORE_DRIVER.root(ws), "manifest.json")
