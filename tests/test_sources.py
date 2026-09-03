"""Source registry and accepted-ref authority tests for V0.3."""

import json
import os
import subprocess
import sys

import pytest

from wikiskill import gates, sources


def _init_repo(tmp_path, name="source"):
    repo = tmp_path / name
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


def _gate_registry():
    argv = (sys.executable, "-c", "raise SystemExit(0)")
    return {
        name: gates.GateProfile(name, argv, 30)
        for name in (
            "registered:test-static",
            "registered:test-build",
            "registered:test-regression",
            "registered:test-performance",
        )
    }


def _manifest(repo, sha, **overrides):
    value = {
        "source_id": "demo-core",
        "adapter": "git_source",
        "repository": str(repo),
        "baseline_ref": "main",
        "baseline_sha": sha,
        "write_policy": {
            "allow": ["src/**"],
            "deny": [".git/**", ".github/**"],
        },
        "patch_policy": {
            "max_files": 2,
            "max_total_lines": 20,
            "text_only": True,
        },
        "gates": {
            "static": "registered:test-static",
            "build": "registered:test-build",
            "regression": "registered:test-regression",
            "performance": None,
        },
    }
    value.update(overrides)
    return value


def _write_manifest(tmp_path, value, name="manifest.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_register_creates_authoritative_ref_and_state(tmp_path):
    repo, sha = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    path = _write_manifest(tmp_path, _manifest(repo, sha))

    registered = sources.register_source(
        ws, str(path), gate_registry=_gate_registry()
    )

    assert registered["source_id"] == "demo-core"
    assert registered["repository"] == os.path.realpath(str(repo))
    assert sources.accepted_sha(ws, "demo-core") == sha
    ref_sha = subprocess.check_output(
        ["git", "rev-parse", "refs/wikiskill/demo-core/accepted"],
        cwd=repo,
        text=True,
    ).strip()
    assert ref_sha == sha
    inspected = sources.inspect_source(ws, "demo-core")
    assert inspected["state"]["accepted_sha"] == sha
    assert inspected["manifest"]["baseline_sha"] == sha
    assert sources.list_sources(ws)[0]["source_id"] == "demo-core"


def test_manifest_unknown_key_and_unknown_gate_fail_closed(tmp_path):
    repo, sha = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    extra = _manifest(repo, sha, unexpected=True)
    with pytest.raises(ValueError, match="unknown source manifest keys"):
        sources.register_source(
            ws, str(_write_manifest(tmp_path, extra, "extra.json")),
            gate_registry=_gate_registry(),
        )

    bad_gate = _manifest(repo, sha)
    bad_gate["gates"]["build"] = "registered:missing"
    with pytest.raises(ValueError, match="unknown registered gate profile"):
        sources.register_source(
            ws, str(_write_manifest(tmp_path, bad_gate, "gate.json")),
            gate_registry=_gate_registry(),
        )


def test_nonexistent_baseline_is_rejected(tmp_path):
    repo, _ = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    bad_sha = "1" * 40
    with pytest.raises(ValueError, match="baseline_sha"):
        sources.register_source(
            ws,
            str(_write_manifest(tmp_path, _manifest(repo, bad_sha))),
            gate_registry=_gate_registry(),
        )


def test_reregister_cannot_change_repository_or_baseline_anchor(tmp_path):
    repo, sha = _init_repo(tmp_path, "source-a")
    repo2, sha2 = _init_repo(tmp_path, "source-b")
    ws = str(tmp_path / "ws")
    registry = _gate_registry()
    sources.register_source(
        ws,
        str(_write_manifest(tmp_path, _manifest(repo, sha), "first.json")),
        gate_registry=registry,
    )

    with pytest.raises(ValueError, match="repository identity"):
        sources.register_source(
            ws,
            str(_write_manifest(tmp_path, _manifest(repo2, sha2), "second.json")),
            gate_registry=registry,
        )

    second_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    changed_anchor = _manifest(repo, second_sha)
    changed_anchor["baseline_sha"] = "2" * 40
    with pytest.raises(ValueError):
        sources.register_source(
            ws,
            str(_write_manifest(tmp_path, changed_anchor, "anchor.json")),
            gate_registry=registry,
        )


def test_state_mismatch_is_repaired_from_git_ref(tmp_path):
    repo, sha = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    sources.register_source(
        ws,
        str(_write_manifest(tmp_path, _manifest(repo, sha))),
        gate_registry=_gate_registry(),
    )
    state_path = tmp_path / "ws" / "sources" / "state" / "demo-core.json"
    state = json.loads(state_path.read_text())
    state["accepted_sha"] = "f" * 40
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert sources.accepted_sha(ws, "demo-core") == sha
    repaired = json.loads(state_path.read_text())
    assert repaired["accepted_sha"] == sha


def test_advance_accepted_sha_uses_compare_and_swap(tmp_path):
    repo, sha = _init_repo(tmp_path)
    ws = str(tmp_path / "ws")
    sources.register_source(
        ws,
        str(_write_manifest(tmp_path, _manifest(repo, sha))),
        gate_registry=_gate_registry(),
    )
    (repo / "src" / "value.txt").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=test@example.invalid",
            "-c", "user.name=test", "commit", "-q", "-m", "next",
        ],
        cwd=repo,
        check=True,
    )
    new_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    state = sources.advance_accepted_sha(ws, "demo-core", sha, new_sha, 3)
    assert state["accepted_sha"] == new_sha
    assert state["previous_sha"] == sha
    assert sources.accepted_sha(ws, "demo-core") == new_sha

    with pytest.raises(sources.SourceOperationalError):
        sources.advance_accepted_sha(ws, "demo-core", sha, new_sha, 4)
