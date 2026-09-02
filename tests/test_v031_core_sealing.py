"""V0.3.1 candidate sealing and reversible source-transition tests."""

import json
import os
import subprocess

import pytest

from wikiskill import core_adapter, sources


def _init_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "value.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=test@example.invalid",
            "-c", "user.name=test", "commit", "-q", "-m", "base",
        ],
        cwd=repo,
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    return repo, sha


def _registered(tmp_path):
    repo, sha = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    manifest = {
        "source_id": "demo-core",
        "adapter": "git_source",
        "repository": str(repo),
        "baseline_ref": "main",
        "baseline_sha": sha,
        "write_policy": {"allow": ["src/**"], "deny": [".git/**", ".github/**"]},
        "patch_policy": {"max_files": 2, "max_total_lines": 20, "text_only": True},
        "gates": {
            "static": "registered:wikiskill-static",
            "build": "registered:wikiskill-build",
            "regression": "registered:wikiskill-regression",
            "performance": None,
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources.register_source(ws, str(path))
    return ws, repo, sha


def _proposal(sha):
    return {
        "target": "core",
        "action": "patch",
        "source_id": "demo-core",
        "base_sha": sha,
        "edits": [
            {
                "file": "src/value.txt",
                "op": "replace",
                "target": "old",
                "content": "new",
            }
        ],
    }


def _session(tmp_path, iteration=3):
    ws, repo, sha = _registered(tmp_path)
    session = core_adapter.begin_candidate(ws, iteration, _proposal(sha))
    core_adapter.apply_candidate(session)
    return ws, repo, sha, session


def _rev(repo, ref):
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def test_seal_creates_candidate_commit_and_temp_ref_without_advancing_accepted(tmp_path):
    ws, repo, base_sha, session = _session(tmp_path)

    sealed = core_adapter.seal_candidate(session, 3)
    candidate_sha = sealed["candidate_sha"]
    assert candidate_sha != base_sha
    assert session.candidate_sha == candidate_sha
    assert os.path.exists(session.worktree)
    assert sources.accepted_sha(ws, "demo-core") == base_sha
    assert _rev(repo, "refs/wikiskill/demo-core/candidate-3") == candidate_sha
    assert _rev(session.worktree, "HEAD") == candidate_sha

    changed = subprocess.check_output(
        ["git", "show", "--pretty=format:", "--name-only", candidate_sha],
        cwd=repo,
        text=True,
    ).splitlines()
    assert [item for item in changed if item.strip()] == ["src/value.txt"]


def test_second_seal_is_rejected(tmp_path):
    _, _, _, session = _session(tmp_path)
    core_adapter.seal_candidate(session, 3)
    with pytest.raises(core_adapter.CoreOperationalError, match="sealed"):
        core_adapter.seal_candidate(session, 3)


def test_advance_removes_worktree_advances_source_and_keeps_candidate_ref(tmp_path):
    ws, repo, base_sha, session = _session(tmp_path)
    sealed = core_adapter.seal_candidate(session, 3)

    transition = core_adapter.advance_accepted_candidate(session, 3)
    assert transition["base_sha"] == base_sha
    assert transition["accepted_sha"] == sealed["candidate_sha"]
    assert not os.path.exists(session.worktree)
    assert sources.accepted_sha(ws, "demo-core") == sealed["candidate_sha"]
    assert _rev(repo, "refs/wikiskill/demo-core/candidate-3") == sealed["candidate_sha"]


def test_compensation_restores_base_then_release_removes_temp_ref(tmp_path):
    ws, repo, base_sha, session = _session(tmp_path)
    sealed = core_adapter.seal_candidate(session, 3)
    transition = core_adapter.advance_accepted_candidate(session, 3)

    restored = core_adapter.compensate_accepted_candidate(session, transition, 3)
    assert restored["restored_sha"] == base_sha
    assert sources.accepted_sha(ws, "demo-core") == base_sha
    assert _rev(repo, "refs/wikiskill/demo-core/candidate-3") == sealed["candidate_sha"]

    released = core_adapter.release_candidate_ref(session, 3)
    assert released["removed"] is True
    assert _rev(repo, "refs/wikiskill/demo-core/candidate-3") is None


def test_reject_sealed_candidate_cleans_worktree_and_temp_ref_without_source_change(tmp_path):
    ws, repo, base_sha, session = _session(tmp_path)
    core_adapter.seal_candidate(session, 3)

    cleanup = core_adapter.reject_candidate(session)
    assert cleanup["removed"] is True
    assert not os.path.exists(session.worktree)
    assert _rev(repo, "refs/wikiskill/demo-core/candidate-3") is None
    assert sources.accepted_sha(ws, "demo-core") == base_sha
