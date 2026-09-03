"""Governed Git Source Adapter tests for V0.3."""

import json
import os
import subprocess
import sys

import pytest

from wikiskill import core_adapter, gates, sources


def _commit(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=test@example.invalid",
            "-c", "user.name=test", "commit", "-q", "-m", message,
        ],
        cwd=repo,
        check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


def _init_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "value.txt").write_text("old\n", encoding="utf-8")
    (repo / "src" / "other.txt").write_text("other\n", encoding="utf-8")
    (repo / ".github").mkdir()
    (repo / ".github" / "work.yml").write_text("safe\n", encoding="utf-8")
    sha = _commit(repo, "base")
    return repo, sha


def _profile(profile_id, code="raise SystemExit(0)"):
    return gates.GateProfile(profile_id, (sys.executable, "-c", code), 30)


def _registry(**codes):
    result = {}
    for name in ("static", "build", "regression", "performance"):
        profile_id = f"registered:test-{name}"
        result[profile_id] = _profile(profile_id, codes.get(name, "raise SystemExit(0)"))
    return result


def _register(tmp_path, *, performance=None, allow=None, deny=None, max_files=2, max_lines=30):
    repo, sha = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    manifest = {
        "source_id": "demo-core",
        "adapter": "git_source",
        "repository": str(repo),
        "baseline_ref": "main",
        "baseline_sha": sha,
        "write_policy": {
            "allow": allow or ["src/**", ".github/**"],
            "deny": deny or [".git/**", ".github/**"],
        },
        "patch_policy": {
            "max_files": max_files,
            "max_total_lines": max_lines,
            "text_only": True,
        },
        "gates": {
            "static": "registered:test-static",
            "build": "registered:test-build",
            "regression": "registered:test-regression",
            "performance": performance,
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources.register_source(ws, str(path), gate_registry=_registry())
    return ws, repo, sha


def _proposal(sha, edits=None, **extra):
    value = {
        "target": "core",
        "action": "patch",
        "source_id": "demo-core",
        "base_sha": sha,
        "edits": edits or [
            {
                "file": "src/value.txt",
                "op": "replace",
                "target": "old",
                "content": "new",
            }
        ],
    }
    value.update(extra)
    return value


def test_core_schema_rejects_control_fields_and_stale_sha(tmp_path):
    ws, _, sha = _register(tmp_path)
    with pytest.raises(ValueError):
        core_adapter.validate_core_proposal(ws, _proposal(sha, command="echo nope"))
    with pytest.raises(ValueError):
        core_adapter.validate_core_proposal(ws, _proposal("f" * 40))


def test_path_policy_rejects_traversal_absolute_deny_and_nonallow(tmp_path):
    ws, _, sha = _register(tmp_path)
    bad_paths = ["../escape.txt", "/tmp/abs.txt", ".github/work.yml", "README.md"]
    for path in bad_paths:
        with pytest.raises(ValueError):
            core_adapter.validate_core_proposal(
                ws,
                _proposal(sha, [{"file": path, "op": "append", "content": "x"}]),
            )


def test_file_and_line_limits_fail_before_worktree(tmp_path):
    ws, _, sha = _register(tmp_path, max_files=1, max_lines=1)
    with pytest.raises(ValueError, match="max_files"):
        core_adapter.validate_core_proposal(
            ws,
            _proposal(
                sha,
                [
                    {"file": "src/value.txt", "op": "append", "content": "x"},
                    {"file": "src/other.txt", "op": "append", "content": "y"},
                ],
            ),
        )
    with pytest.raises(ValueError, match="line"):
        core_adapter.validate_core_proposal(
            ws,
            _proposal(sha, [{"file": "src/value.txt", "op": "append", "content": "x\ny"}]),
        )
    assert not os.path.exists(os.path.join(ws, "runs", "core-worktrees"))


def test_symlink_and_non_utf8_blob_are_rejected(tmp_path):
    ws, repo, _ = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    os.symlink("value.txt", repo / "src" / "link.txt")
    (repo / "src" / "binary.dat").write_bytes(b"\xff\x00\x01")
    sha = _commit(repo, "unsafe fixtures")
    ref = sources.accepted_ref("demo-core")
    subprocess.run(["git", "update-ref", ref, sha], cwd=repo, check=True)

    with pytest.raises(ValueError, match="symlink|mode"):
        core_adapter.validate_core_proposal(
            ws, _proposal(sha, [{"file": "src/link.txt", "op": "append", "content": "x"}])
        )
    with pytest.raises(ValueError, match="text|UTF-8|binary"):
        core_adapter.validate_core_proposal(
            ws, _proposal(sha, [{"file": "src/binary.dat", "op": "append", "content": "x"}])
        )


def test_replace_target_must_be_unique(tmp_path):
    ws, repo, _ = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    (repo / "src" / "value.txt").write_text("old old\n", encoding="utf-8")
    sha = _commit(repo, "duplicate target")
    subprocess.run(
        ["git", "update-ref", sources.accepted_ref("demo-core"), sha], cwd=repo, check=True
    )
    with pytest.raises(ValueError, match="exactly once"):
        core_adapter.validate_core_proposal(ws, _proposal(sha))


def test_candidate_uses_isolated_worktree_and_preserves_source_tree(tmp_path):
    ws, repo, sha = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    session = core_adapter.begin_candidate(ws, 1, _proposal(sha))
    assert session.worktree.startswith(os.path.join(ws, "runs", "core-worktrees"))
    assert os.path.realpath(session.worktree) != os.path.realpath(str(repo))

    desc = core_adapter.apply_candidate(session)
    assert "demo-core" in desc
    assert (repo / "src" / "value.txt").read_text(encoding="utf-8") == "old\n"
    assert os.path.exists(session.worktree)
    assert "new" in core_adapter.candidate_diff(session)

    cleanup = core_adapter.reject_candidate(session)
    assert cleanup["removed"] is True
    assert not os.path.exists(session.worktree)
    assert sources.accepted_sha(ws, "demo-core") == sha


def test_pre_gates_short_circuit_in_order(tmp_path):
    ws, _, sha = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    session = core_adapter.begin_candidate(ws, 1, _proposal(sha))
    core_adapter.apply_candidate(session)
    registry = _registry(static="raise SystemExit(4)")
    results = core_adapter.run_pre_gates(session, gate_registry=registry)
    assert [r["gate"] for r in results] == ["static"]
    assert results[-1]["status"] == "fail"
    core_adapter.reject_candidate(session)

    session = core_adapter.begin_candidate(ws, 2, _proposal(sha))
    core_adapter.apply_candidate(session)
    registry = _registry(build="raise SystemExit(5)")
    results = core_adapter.run_pre_gates(session, gate_registry=registry)
    assert [r["gate"] for r in results] == ["static", "build"]
    assert results[-1]["status"] == "fail"
    core_adapter.reject_candidate(session)


def test_performance_not_configured_is_structured_result(tmp_path):
    ws, _, sha = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    session = core_adapter.begin_candidate(ws, 1, _proposal(sha))
    core_adapter.apply_candidate(session)
    results = core_adapter.run_pre_gates(session, gate_registry=_registry())
    assert [r["gate"] for r in results] == [
        "static", "build", "regression", "performance"
    ]
    assert results[-1]["status"] == "not_configured"
    core_adapter.reject_candidate(session)


def test_gate_launch_error_is_operational(tmp_path):
    ws, _, sha = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    session = core_adapter.begin_candidate(ws, 1, _proposal(sha))
    core_adapter.apply_candidate(session)
    registry = _registry()
    registry["registered:test-static"] = gates.GateProfile(
        "registered:test-static", ("__missing_gate_binary__",), 30
    )
    with pytest.raises(core_adapter.CoreOperationalError):
        core_adapter.run_pre_gates(session, gate_registry=registry)
    core_adapter.reject_candidate(session)


def test_accept_commits_advances_ref_and_removes_worktree(tmp_path):
    ws, repo, sha = _register(tmp_path, allow=["src/**"], deny=[".git/**"])
    session = core_adapter.begin_candidate(ws, 3, _proposal(sha))
    core_adapter.apply_candidate(session)
    results = core_adapter.run_pre_gates(session, gate_registry=_registry())
    assert all(r["status"] in ("pass", "not_configured") for r in results)

    accepted = core_adapter.accept_candidate(session, 3)
    assert accepted["accepted_sha"] != sha
    assert sources.accepted_sha(ws, "demo-core") == accepted["accepted_sha"]
    assert not os.path.exists(session.worktree)
    assert subprocess.check_output(
        ["git", "cat-file", "-t", accepted["accepted_sha"]], cwd=repo, text=True
    ).strip() == "commit"
