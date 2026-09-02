"""V0.3.1 trusted runtime binding registry and operator-state tests."""

import json
import os
import subprocess

import pytest

from wikiskill import gating, runtime_bindings, sources


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


def _register_source(tmp_path, *, source_id="demo-core", repo_name="source"):
    repo, sha = _init_repo(tmp_path, repo_name)
    ws = str(tmp_path / "ws")
    manifest = {
        "source_id": source_id,
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
    path = tmp_path / f"{source_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources.register_source(ws, str(path))
    return ws, repo, sha


def test_runtime_bind_and_activate_before_baseline(tmp_path):
    ws, _, sha = _register_source(tmp_path)

    bound = runtime_bindings.bind_source(
        ws, "demo-core", "registered:python-json-runner-v1"
    )
    assert bound == {
        "source_id": "demo-core",
        "binding_profile": "registered:python-json-runner-v1",
        "role": "inference",
    }

    active = runtime_bindings.activate_source(ws, "demo-core")
    assert active["source_id"] == "demo-core"
    assert active["accepted_sha"] == sha
    assert active["binding_profile"] == "registered:python-json-runner-v1"
    assert active["fingerprint"].startswith("sha256:")

    cfg = runtime_bindings.active_runtime_config(ws)
    assert cfg["source_id"] == "demo-core"
    assert cfg["binding_profile"] == "registered:python-json-runner-v1"


def test_unknown_source_and_profile_fail_closed(tmp_path):
    ws, _, _ = _register_source(tmp_path)
    with pytest.raises(ValueError, match="unknown source_id"):
        runtime_bindings.bind_source(
            ws, "missing", "registered:python-json-runner-v1"
        )
    with pytest.raises(ValueError, match="unknown runtime binding profile"):
        runtime_bindings.bind_source(ws, "demo-core", "registered:missing")


def test_activation_after_scored_baseline_fails_closed(tmp_path):
    ws, _, _ = _register_source(tmp_path)
    runtime_bindings.bind_source(
        ws, "demo-core", "registered:python-json-runner-v1"
    )
    gating.save_state(
        ws,
        {
            "domain": "ws",
            "baseline": 0.0,
            "r_best": 0.0,
            "next_iter": 1,
            "history": [],
        },
    )
    with pytest.raises(ValueError, match="unscored workspace"):
        runtime_bindings.activate_source(ws, "demo-core")


def test_runtime_fingerprint_is_deterministic_and_identity_sensitive():
    a = runtime_bindings.runtime_fingerprint(
        "demo-core",
        "registered:python-json-runner-v1",
        "a" * 40,
        "wikiskill-runtime-v1",
    )
    b = runtime_bindings.runtime_fingerprint(
        "demo-core",
        "registered:python-json-runner-v1",
        "a" * 40,
        "wikiskill-runtime-v1",
    )
    c = runtime_bindings.runtime_fingerprint(
        "demo-core",
        "registered:python-json-runner-v1",
        "b" * 40,
        "wikiskill-runtime-v1",
    )
    assert a == b
    assert a != c
    assert a.startswith("sha256:") and len(a) == 71


def test_runtime_inspect_validate_are_bounded_and_malformed_state_fails(tmp_path):
    ws, _, sha = _register_source(tmp_path)
    runtime_bindings.bind_source(
        ws, "demo-core", "registered:python-json-runner-v1"
    )
    runtime_bindings.activate_source(ws, "demo-core")

    inspected = runtime_bindings.inspect_runtime(ws)
    validated = runtime_bindings.validate_runtime(ws)
    text = json.dumps({"inspect": inspected, "validate": validated}, sort_keys=True)
    assert "demo-core" in text
    assert sha in text
    assert "registered:python-json-runner-v1" in text
    for forbidden in ("argv", "entrypoint", "environment", "credential", "secret"):
        assert forbidden not in text.lower()

    registrations = os.path.join(ws, "runtime", "registrations.json")
    data = json.loads(open(registrations, encoding="utf-8").read())
    data["command"] = "echo unsafe"
    with open(registrations, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    with pytest.raises(ValueError, match="runtime registrations"):
        runtime_bindings.validate_runtime(ws)
