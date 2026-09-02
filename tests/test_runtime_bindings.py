"""V0.3.1 trusted runtime binding registry, worktree, and protocol tests."""

import json
import os
import subprocess
import sys

import pytest

from wikiskill import core_adapter, gating, runtime_bindings, sources


def _runtime_script(answer="BOUND", *, response='{"status":"ok"}', sleep_s=0, exit_code=0):
    return f'''import json\nimport os\nimport sys\nimport time\n\nrequest = json.loads(sys.stdin.readline())\nif {sleep_s!r}:\n    time.sleep({sleep_s!r})\nworkdir = request["workdir"]\nos.makedirs(workdir, exist_ok=True)\nsecret = os.environ.get("WIKISKILL_TEST_SECRET", "<missing>")\nwith open(os.path.join(workdir, "out.txt"), "w", encoding="utf-8") as handle:\n    handle.write({answer!r} + "|secret=" + secret)\nprint({response!r})\nraise SystemExit({exit_code!r})\n'''


def _init_repo(tmp_path, name="source", runtime_script=None):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "value.txt").write_text("old\n", encoding="utf-8")
    if runtime_script is not None:
        (repo / "wikiskill_runtime.py").write_text(runtime_script, encoding="utf-8")
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


def _register_source(
    tmp_path,
    *,
    source_id="demo-core",
    repo_name="source",
    runtime_script=None,
):
    repo, sha = _init_repo(tmp_path, repo_name, runtime_script=runtime_script)
    ws = str(tmp_path / "ws")
    manifest = {
        "source_id": source_id,
        "adapter": "git_source",
        "repository": str(repo),
        "baseline_ref": "main",
        "baseline_sha": sha,
        "write_policy": {
            "allow": ["src/**", "wikiskill_runtime.py"],
            "deny": [".git/**", ".github/**"],
        },
        "patch_policy": {"max_files": 2, "max_total_lines": 200, "text_only": True},
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


def _activate(
    ws,
    *,
    source_id="demo-core",
    profile_id="registered:python-json-runner-v1",
    registry=None,
):
    runtime_bindings.bind_source(ws, source_id, profile_id, registry=registry)
    return runtime_bindings.activate_source(ws, source_id, registry=registry)


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


def test_accepted_binding_uses_detached_accepted_sha_not_dirty_checkout(tmp_path, monkeypatch):
    ws, repo, sha = _register_source(
        tmp_path, runtime_script=_runtime_script("ACCEPTED")
    )
    _activate(ws)
    (repo / "wikiskill_runtime.py").write_text(
        _runtime_script("DIRTY_CHECKOUT"), encoding="utf-8"
    )
    monkeypatch.setenv("WIKISKILL_TEST_SECRET", "do-not-forward")

    bound = runtime_bindings.bind_active_accepted(ws)
    assert isinstance(bound, runtime_bindings.BoundRuntimeSession)
    assert bound.source_sha == sha
    assert os.path.realpath(bound.worktree) != os.path.realpath(str(repo))
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=bound.worktree, text=True
    ).strip() == sha

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    result = bound.runner(
        ws,
        "prompt",
        tag="iter-00/val/t1",
        workdir=str(sandbox),
        max_turns=15,
    )
    assert result["exit_code"] == 0
    assert result["runtime"]["source_sha"] == sha
    assert (sandbox / "out.txt").read_text() == "ACCEPTED|secret=<missing>"
    evidence = bound.evidence
    assert evidence["source_sha"] == sha
    assert evidence["binding_profile"] == "registered:python-json-runner-v1"
    assert evidence["fingerprint"].startswith("sha256:")
    assert evidence["entrypoint_blob_sha"]

    closed = bound.close()
    assert closed["closed"] is True
    assert not os.path.exists(bound.worktree)
    assert bound.close()["closed"] is True


def test_missing_entrypoint_fails_closed(tmp_path):
    ws, _, _ = _register_source(tmp_path, runtime_script=None)
    _activate(ws)
    with pytest.raises(ValueError, match="entrypoint"):
        runtime_bindings.bind_active_accepted(ws)


def test_candidate_binding_requires_exact_head_and_does_not_own_worktree(tmp_path):
    ws, _, base_sha = _register_source(
        tmp_path, runtime_script=_runtime_script("OLD")
    )
    _activate(ws)
    proposal = {
        "target": "core",
        "action": "patch",
        "source_id": "demo-core",
        "base_sha": base_sha,
        "edits": [
            {
                "file": "wikiskill_runtime.py",
                "op": "replace",
                "target": "OLD",
                "content": "NEW",
            }
        ],
    }
    candidate = core_adapter.begin_candidate(ws, 1, proposal)
    core_adapter.apply_candidate(candidate)
    sealed = core_adapter.seal_candidate(candidate, 1)

    with pytest.raises(runtime_bindings.RuntimeBindingError, match="HEAD|SHA"):
        runtime_bindings.bind_sha(
            ws,
            "demo-core",
            base_sha,
            candidate_worktree=candidate.worktree,
        )

    bound = runtime_bindings.bind_sha(
        ws,
        "demo-core",
        sealed["candidate_sha"],
        candidate_worktree=candidate.worktree,
    )
    assert bound.source_sha == sealed["candidate_sha"]
    assert os.path.exists(candidate.worktree)
    assert bound.close()["closed"] is True
    assert os.path.exists(candidate.worktree)
    assert bound.close()["closed"] is True
    core_adapter.reject_candidate(candidate)
    assert not os.path.exists(candidate.worktree)


@pytest.mark.parametrize(
    "script, error",
    [
        (_runtime_script(response="not-json"), "JSON"),
        (_runtime_script(response='{"status":"bad"}'), "status"),
        (_runtime_script(exit_code=7), "exit"),
    ],
)
def test_python_runtime_protocol_failures_are_operational(tmp_path, script, error):
    ws, _, _ = _register_source(tmp_path, runtime_script=script)
    _activate(ws)
    bound = runtime_bindings.bind_active_accepted(ws)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    try:
        with pytest.raises(runtime_bindings.RuntimeBindingError, match=error):
            bound.runner(
                ws,
                "prompt",
                tag="iter-00/val/t1",
                workdir=str(sandbox),
                max_turns=15,
            )
    finally:
        bound.close()


def test_multiple_json_objects_are_rejected(tmp_path):
    script = '''import json, os, sys\nrequest=json.loads(sys.stdin.readline())\nprint(json.dumps({"status":"ok"}))\nprint(json.dumps({"status":"ok"}))\n'''
    ws, _, _ = _register_source(tmp_path, runtime_script=script)
    _activate(ws)
    bound = runtime_bindings.bind_active_accepted(ws)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    try:
        with pytest.raises(runtime_bindings.RuntimeBindingError, match="JSON|response"):
            bound.runner(
                ws,
                "prompt",
                tag="iter-00/val/t1",
                workdir=str(sandbox),
                max_turns=15,
            )
    finally:
        bound.close()


def test_python_runtime_timeout_and_missing_sandbox_fail_closed(tmp_path):
    profile_id = "registered:test-python-timeout"
    registry = {
        profile_id: runtime_bindings.RuntimeBindingProfile(
            profile_id=profile_id,
            protocol="wikiskill-runtime-v1",
            timeout_s=1,
            entrypoint="wikiskill_runtime.py",
        )
    }
    ws, _, _ = _register_source(
        tmp_path, runtime_script=_runtime_script("SLOW", sleep_s=2)
    )
    _activate(ws, profile_id=profile_id, registry=registry)
    bound = runtime_bindings.bind_active_accepted(ws, registry=registry)
    try:
        with pytest.raises(runtime_bindings.RuntimeBindingError, match="sandbox"):
            bound.runner(
                ws,
                "prompt",
                tag="iter-00/val/t1",
                workdir=str(tmp_path / "missing-sandbox"),
                max_turns=15,
            )
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        with pytest.raises(runtime_bindings.RuntimeBindingError, match="timeout|timed out"):
            bound.runner(
                ws,
                "prompt",
                tag="iter-00/val/t1",
                workdir=str(sandbox),
                max_turns=15,
            )
    finally:
        bound.close()
