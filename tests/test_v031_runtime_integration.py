"""V0.3.1 real-Git runtime comparability and Core causal-evolution tests."""

import json
import os
import subprocess

import pytest

from wikiskill import (
    core_adapter,
    gating,
    harness,
    runtime_bindings,
    sources,
    tasks as tasks_mod,
    traces,
)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _rev(repo, ref):
    proc = _git(repo, "rev-parse", "--verify", ref)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _runtime_source(tmp_path, answer="wrong", name="runtime-source"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "wikiskill").mkdir()
    (repo / "wikiskill" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    (repo / "wikiskill_runtime.py").write_text(
        _runtime_script(answer), encoding="utf-8"
    )
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


def _runtime_script(answer):
    return (
        "import json\n"
        "import os\n"
        "import sys\n"
        f'ANSWER = {answer!r}\n'
        "request = json.loads(sys.stdin.readline())\n"
        "with open(os.path.join(request['workdir'], 'out.txt'), 'w', encoding='utf-8') as handle:\n"
        "    handle.write(ANSWER)\n"
        "print(json.dumps({'status': 'ok'}))\n"
    )


def _task(task_id, split):
    return {
        "id": task_id,
        "split": split,
        "title": task_id,
        "prompt": "write ok",
        "sandbox": {"input.txt": "fixture"},
        "grader": {"type": "exact", "file": "out.txt", "expected": "ok"},
    }


def _setup_workspace(tmp_path, *, active=True, answer="wrong"):
    ws = str(tmp_path / "ws")
    harness.init_workspace(ws)
    tasks_mod.save(ws, [_task("train-one", "train"), _task("val-one", "val")])

    repo, sha = _runtime_source(tmp_path, answer=answer)
    manifest = {
        "source_id": "demo-core",
        "adapter": "git_source",
        "repository": str(repo),
        "baseline_ref": "main",
        "baseline_sha": sha,
        "write_policy": {
            "allow": ["wikiskill_runtime.py"],
            "deny": [".git/**", ".github/**"],
        },
        "patch_policy": {
            "max_files": 1,
            "max_total_lines": 100,
            "text_only": True,
        },
        "gates": {
            "static": "registered:wikiskill-static",
            "build": "registered:wikiskill-build",
            "regression": "registered:wikiskill-regression",
            "performance": None,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sources.register_source(ws, str(manifest_path))
    if active:
        runtime_bindings.bind_source(
            ws, "demo-core", "registered:python-json-runner-v1"
        )
        runtime_bindings.activate_source(ws, "demo-core")
    return ws, repo, sha


class FrameworkRunner:
    def __init__(self, task_answer="framework"):
        self.task_answer = task_answer
        self.calls = []

    def __call__(self, ws, prompt, *, tag, workdir=None, **kwargs):
        self.calls.append(tag)
        if tag.startswith("iter-") and workdir:
            with open(os.path.join(workdir, "out.txt"), "w", encoding="utf-8") as handle:
                handle.write(self.task_answer)
        return {
            "exit_code": 0,
            "duration_s": 0.01,
            "stdout_path": None,
            "session_file": None,
        }


def _core_proposal(base_sha, answer="ok"):
    return {
        "target": "core",
        "action": "patch",
        "source_id": "demo-core",
        "base_sha": base_sha,
        "edits": [
            {
                "file": "wikiskill_runtime.py",
                "op": "replace",
                "target": "ANSWER = 'wrong'",
                "content": f"ANSWER = {answer!r}",
            }
        ],
    }


def _prompt_proposal():
    return {
        "target": "prompt",
        "action": "create",
        "name": "inference",
        "content": "candidate prompt overlay",
    }


def _stub_roles(monkeypatch, framework, proposal_for_iteration):
    role_calls = []

    def maintain(ws, k, sampled, runner=None, **kwargs):
        assert runner is framework
        role_calls.append(("maintain", k))
        return {}

    def propose(ws, k, train_results, runner=None, **kwargs):
        assert runner is framework
        role_calls.append(("propose", k))
        return proposal_for_iteration(k), {}

    monkeypatch.setattr(harness, "maintain_step", maintain)
    monkeypatch.setattr(harness, "propose_step", propose)
    return role_calls


def _runtime_meta(ws, iteration, split, task_id):
    return traces.load_trace(ws, iteration, split, task_id).get("runtime")


def _candidate_ref(iteration=1):
    return f"refs/wikiskill/demo-core/candidate-{iteration}"


def test_active_runtime_owns_baseline_and_records_identity(monkeypatch, tmp_path):
    ws, _, sha = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="framework")

    state = harness.evolve(ws, iters=0, runner=framework, verbose=False)

    assert state["baseline"] == 0.0
    assert state["r_best"] == 0.0
    assert state["runtime_identity"] == {
        "source_id": "demo-core",
        "binding_profile": "registered:python-json-runner-v1",
        "accepted_sha": sha,
        "fingerprint": runtime_bindings.active_runtime_config(ws)["fingerprint"],
    }
    meta = _runtime_meta(ws, 0, "val", "val-one")
    assert meta["source_sha"] == sha
    assert meta["source_id"] == "demo-core"
    assert not any(tag.startswith("iter-00/") for tag in framework.calls)


def test_no_active_runtime_preserves_legacy_runner(monkeypatch, tmp_path):
    ws, _, _ = _setup_workspace(tmp_path, active=False, answer="wrong")
    framework = FrameworkRunner(task_answer="ok")

    state = harness.evolve(ws, iters=0, runner=framework, verbose=False)

    assert state["baseline"] == 1.0
    assert "runtime_identity" not in state
    assert any(tag.startswith("iter-00/val/") for tag in framework.calls)
    assert _runtime_meta(ws, 0, "val", "val-one") is None


def test_non_core_candidate_held_out_uses_same_accepted_runtime(monkeypatch, tmp_path):
    ws, _, sha = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="ok")
    _stub_roles(monkeypatch, framework, lambda k: _prompt_proposal())

    state = harness.evolve(ws, iters=1, runner=framework, verbose=False)

    assert _runtime_meta(ws, 1, "train", "train-one")["source_sha"] == sha
    assert _runtime_meta(ws, 1, "val", "val-one")["source_sha"] == sha
    assert state["history"][-1]["target"] == "prompt"
    assert state["history"][-1]["status"] == "rejected"
    assert not any(tag.startswith("iter-") for tag in framework.calls)


def test_core_candidate_real_runtime_causes_acceptance(monkeypatch, tmp_path):
    ws, repo, sha_a = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="framework")
    _stub_roles(monkeypatch, framework, lambda k: _core_proposal(sha_a, "ok"))

    state = harness.evolve(ws, iters=1, runner=framework, verbose=False)

    sha_b = sources.accepted_sha(ws, "demo-core")
    assert sha_b != sha_a
    item = state["history"][-1]
    assert item["status"] == "accepted"
    assert item["accepted"] is True
    assert item["r_val"] == 1.0
    assert state["r_best"] == 1.0
    assert state["runtime_identity"]["accepted_sha"] == sha_b
    assert _runtime_meta(ws, 0, "val", "val-one")["source_sha"] == sha_a
    assert _runtime_meta(ws, 1, "val", "val-one")["source_sha"] == sha_b
    assert _rev(repo, _candidate_ref(1)) is None
    assert "ANSWER = 'wrong'" in (repo / "wikiskill_runtime.py").read_text()
    assert not any(tag.startswith("iter-") for tag in framework.calls)


def test_core_non_improvement_executes_candidate_then_rejects(monkeypatch, tmp_path):
    ws, repo, sha_a = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="ok")
    _stub_roles(monkeypatch, framework, lambda k: _core_proposal(sha_a, "still-wrong"))

    state = harness.evolve(ws, iters=1, runner=framework, verbose=False)

    item = state["history"][-1]
    assert item["status"] == "rejected"
    assert item["r_val"] == 0.0
    candidate_sha = _runtime_meta(ws, 1, "val", "val-one")["source_sha"]
    assert candidate_sha != sha_a
    assert sources.accepted_sha(ws, "demo-core") == sha_a
    assert state["runtime_identity"]["accepted_sha"] == sha_a
    assert state["r_best"] == 0.0
    assert _rev(repo, _candidate_ref(1)) is None


def test_next_iteration_rebinds_newly_accepted_runtime(monkeypatch, tmp_path):
    ws, _, sha_a = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="framework")

    def proposal(k):
        if k == 1:
            return _core_proposal(sha_a, "ok")
        return {"action": "no_action"}

    _stub_roles(monkeypatch, framework, proposal)
    state = harness.evolve(
        ws, iters=2, runner=framework, verbose=False, no_early_stop=True
    )

    sha_b = state["runtime_identity"]["accepted_sha"]
    assert sha_b != sha_a
    assert _runtime_meta(ws, 2, "train", "train-one")["source_sha"] == sha_b
    assert traces.load_trace(ws, 2, "train", "train-one")["score"] == 1.0


def test_state_save_failure_after_source_transition_compensates_to_A(monkeypatch, tmp_path):
    ws, repo, sha_a = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="framework")
    harness.evolve(ws, iters=0, runner=framework, verbose=False)
    _stub_roles(monkeypatch, framework, lambda k: _core_proposal(sha_a, "ok"))

    real_save = gating.save_state
    failed = {"done": False}

    def fail_once(got_ws, state):
        identity = state.get("runtime_identity") or {}
        if (
            not failed["done"]
            and state.get("r_best") == 1.0
            and identity.get("accepted_sha") not in (None, sha_a)
        ):
            failed["done"] = True
            raise OSError("simulated final scoring-state failure")
        return real_save(got_ws, state)

    monkeypatch.setattr(gating, "save_state", fail_once)
    state = harness.evolve(ws, iters=1, runner=framework, verbose=False)

    persisted = gating.load_state(ws)
    assert failed["done"] is True
    assert sources.accepted_sha(ws, "demo-core") == sha_a
    assert persisted["r_best"] == 0.0
    assert persisted["runtime_identity"]["accepted_sha"] == sha_a
    assert state["history"][-1]["status"] == "operational_error"
    assert _rev(repo, _candidate_ref(1)) is not None


def test_compensation_failure_enters_recovery_required_and_blocks_scoring(monkeypatch, tmp_path):
    ws, repo, sha_a = _setup_workspace(tmp_path, active=True, answer="wrong")
    framework = FrameworkRunner(task_answer="framework")
    harness.evolve(ws, iters=0, runner=framework, verbose=False)
    _stub_roles(monkeypatch, framework, lambda k: _core_proposal(sha_a, "ok"))

    real_save = gating.save_state
    failed = {"done": False}

    def fail_once(got_ws, state):
        identity = state.get("runtime_identity") or {}
        if (
            not failed["done"]
            and state.get("r_best") == 1.0
            and identity.get("accepted_sha") not in (None, sha_a)
        ):
            failed["done"] = True
            raise OSError("simulated final scoring-state failure")
        return real_save(got_ws, state)

    monkeypatch.setattr(gating, "save_state", fail_once)

    def no_compensation(*args, **kwargs):
        raise core_adapter.CoreOperationalError("simulated compensation failure")

    monkeypatch.setattr(core_adapter, "compensate_accepted_candidate", no_compensation)
    state = harness.evolve(ws, iters=1, runner=framework, verbose=False)

    sha_b = sources.accepted_sha(ws, "demo-core")
    persisted = gating.load_state(ws)
    assert sha_b != sha_a
    assert persisted["runtime_identity"]["accepted_sha"] == sha_a
    assert persisted["r_best"] == 0.0
    assert state["history"][-1]["status"] == "recovery_required"
    assert _rev(repo, _candidate_ref(1)) == sha_b

    with pytest.raises(runtime_bindings.RuntimeBindingError, match="mismatch|recovery|accepted"):
        harness.evolve(ws, iters=1, runner=framework, verbose=False)
