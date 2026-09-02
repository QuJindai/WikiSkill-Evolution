"""V0.3.1 atomic gating state, runtime trace, CLI, and run-task contracts."""

import json
import os

import pytest

from wikiskill import cli, gating, prompts, runtime_bindings, traces


def _task():
    return {
        "id": "t1",
        "split": "val",
        "title": "T",
        "prompt": "write output",
        "sandbox": {"input.txt": "fixture"},
        "grader": {"type": "exact", "file": "out.txt", "expected": "ok"},
    }


def test_save_state_is_atomic_when_replace_fails(monkeypatch, tmp_path):
    ws = str(tmp_path / "ws")
    old = {
        "domain": "ws",
        "baseline": 0.0,
        "r_best": 0.0,
        "next_iter": 1,
        "history": [],
    }
    gating.save_state(ws, old)
    state_file = gating.state_path(ws)
    before = open(state_file, encoding="utf-8").read()

    real_replace = os.replace

    def fail_replace(src, dst):
        if os.path.realpath(dst) == os.path.realpath(state_file):
            raise OSError("simulated atomic replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(gating.os, "replace", fail_replace)
    new = {**old, "r_best": 1.0, "history": [{"status": "accepted"}]}
    with pytest.raises(OSError, match="atomic replace failure"):
        gating.save_state(ws, new)

    assert open(state_file, encoding="utf-8").read() == before
    assert gating.load_state(ws) == old
    parent = os.path.dirname(state_file)
    assert not [name for name in os.listdir(parent) if ".tmp-" in name]


def test_run_task_persists_only_bounded_runtime_identity(tmp_path):
    ws = str(tmp_path / "ws")
    task = _task()

    def runner(got_ws, prompt, *, tag, workdir=None, **kwargs):
        assert got_ws == ws
        with open(os.path.join(workdir, "out.txt"), "w", encoding="utf-8") as handle:
            handle.write("ok")
        return {
            "exit_code": 0,
            "duration_s": 0.01,
            "stdout_path": None,
            "session_file": None,
        }

    evidence = {
        "source_id": "demo-core",
        "source_sha": "a" * 40,
        "binding_profile": "registered:python-json-runner-v1",
        "fingerprint": "sha256:" + "b" * 64,
        "command": "must-not-persist",
        "env": {"SECRET": "must-not-persist"},
        "worktree": "/must/not/persist",
    }
    result = gating.run_task(
        ws,
        task,
        1,
        runner=runner,
        overwrite=True,
        runtime_evidence=evidence,
    )
    assert result["score"] == 1.0
    meta = traces.load_trace(ws, 1, "val", "t1")
    assert meta["runtime"] == {
        "source_id": "demo-core",
        "source_sha": "a" * 40,
        "binding_profile": "registered:python-json-runner-v1",
        "fingerprint": "sha256:" + "b" * 64,
    }
    serialized = json.dumps(meta, sort_keys=True)
    assert "must-not-persist" not in serialized
    assert "worktree" not in serialized


def test_run_gate_forwards_same_runtime_identity_to_every_task(monkeypatch, tmp_path):
    ws = str(tmp_path / "ws")
    seen = []
    identity = {
        "source_id": "demo-core",
        "source_sha": "a" * 40,
        "binding_profile": "registered:python-json-runner-v1",
        "fingerprint": "sha256:" + "b" * 64,
    }

    def fake_run_task(got_ws, task, it, **kwargs):
        seen.append(kwargs.get("runtime_evidence"))
        return {**task, "score": 1.0, "result": {}}

    monkeypatch.setattr(gating, "run_task", fake_run_task)
    tasks = [_task(), {**_task(), "id": "t2"}]
    result = gating.run_gate(ws, tasks, 2, runtime_evidence=identity)
    assert result["mean"] == 1.0
    assert seen == [identity, identity]


def test_runtime_cli_routes_operator_operations(monkeypatch, tmp_path, capsys):
    ws = str(tmp_path / "ws")
    calls = []
    identity = {
        "source_id": "demo-core",
        "binding_profile": "registered:python-json-runner-v1",
        "accepted_sha": "a" * 40,
        "fingerprint": "sha256:" + "b" * 64,
        "role": "inference",
    }
    monkeypatch.setattr(
        runtime_bindings,
        "bind_source",
        lambda got_ws, source_id, profile_id: calls.append(
            ("bind", got_ws, source_id, profile_id)
        )
        or {"source_id": source_id, "binding_profile": profile_id, "role": "inference"},
    )
    monkeypatch.setattr(
        runtime_bindings,
        "activate_source",
        lambda got_ws, source_id: calls.append(("activate", got_ws, source_id))
        or identity,
    )
    monkeypatch.setattr(
        runtime_bindings,
        "inspect_runtime",
        lambda got_ws: calls.append(("inspect", got_ws))
        or {"version": 1, "registrations": [identity], "active": identity},
    )
    monkeypatch.setattr(
        runtime_bindings,
        "validate_runtime",
        lambda got_ws: calls.append(("validate", got_ws))
        or {"status": "valid", "version": 1, "registrations": [identity], "active": identity},
    )

    assert cli.main([
        "runtime", "bind", "demo-core", "registered:python-json-runner-v1", "--ws", ws
    ]) == 0
    assert cli.main(["runtime", "activate", "demo-core", "--ws", ws]) == 0
    assert cli.main(["runtime", "inspect", "--ws", ws]) == 0
    assert cli.main(["runtime", "validate", "--ws", ws]) == 0

    assert ("bind", ws, "demo-core", "registered:python-json-runner-v1") in calls
    assert ("activate", ws, "demo-core") in calls
    assert ("inspect", ws) in calls
    assert ("validate", ws) in calls
    out = capsys.readouterr().out
    assert "demo-core" in out
    assert "registered:python-json-runner-v1" in out
    assert "valid" in out


def test_runtime_cli_has_no_arbitrary_execution_surfaces():
    with pytest.raises(SystemExit):
        cli.main(["runtime", "exec", "echo danger"])
    with pytest.raises(SystemExit):
        cli.main(["runtime", "register-command", "danger"])


def test_run_task_cli_uses_active_runtime_and_closes_it(monkeypatch, tmp_path):
    ws = str(tmp_path / "ws")
    task = _task()
    fake_runner = object()
    evidence = {
        "source_id": "demo-core",
        "source_sha": "a" * 40,
        "binding_profile": "registered:python-json-runner-v1",
        "fingerprint": "sha256:" + "b" * 64,
    }
    events = []

    class FakeBound:
        runner = fake_runner
        evidence = evidence

        def close(self):
            events.append("close")
            return {"closed": True}

    monkeypatch.setattr(cli.tasks_mod, "load", lambda got_ws: [task])
    monkeypatch.setattr(
        runtime_bindings,
        "bind_active_accepted",
        lambda got_ws: events.append(("bind", got_ws)) or FakeBound(),
    )

    def fake_run_task(got_ws, got_task, it, **kwargs):
        events.append(("run", got_ws, got_task["id"], it, kwargs))
        assert kwargs["runner"] is fake_runner
        assert kwargs["runtime_evidence"] == evidence
        return {**got_task, "score": 1.0, "result": {}}

    monkeypatch.setattr(cli.gating, "run_task", fake_run_task)
    assert cli.main(["run-task", "demo", "t1", "--ws", ws]) == 0
    assert events[0] == ("bind", ws)
    assert events[-1] == "close"


def test_run_task_cli_preserves_legacy_runner_when_no_runtime(monkeypatch, tmp_path):
    ws = str(tmp_path / "ws")
    task = _task()
    seen = []
    monkeypatch.setattr(cli.tasks_mod, "load", lambda got_ws: [task])
    monkeypatch.setattr(runtime_bindings, "bind_active_accepted", lambda got_ws: None)

    def fake_run_task(got_ws, got_task, it, **kwargs):
        seen.append(kwargs)
        return {**got_task, "score": 0.0, "result": {}}

    monkeypatch.setattr(cli.gating, "run_task", fake_run_task)
    assert cli.main(["run-task", "demo", "t1", "--ws", ws]) == 0
    assert "runtime_evidence" not in seen[0] or seen[0]["runtime_evidence"] is None
    assert callable(seen[0]["runner"])


def test_proposer_cannot_select_runtime_binding_profile(tmp_path):
    text = prompts.proposer_prompt(str(tmp_path), 1, [])
    lowered = text.lower()
    assert "runtime binding" in lowered
    assert "trusted operator" in lowered or "operator-controlled" in lowered
    assert "binding_profile" in lowered
    assert "must not" in lowered or "cannot" in lowered
    assert '"target": "core"' in text
    assert '"source_id"' in text and '"base_sha"' in text
