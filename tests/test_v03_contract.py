"""V0.3 core-driver integration and compatibility contracts."""

import os


from wikiskill import assets, core_adapter, gating, harness, prompts
from wikiskill import tasks as tasks_mod, wiki


def _workspace(tmp_path):
    ws = str(tmp_path / "ws")
    harness.init_workspace(ws)
    task = {
        "id": "train-one",
        "split": "train",
        "title": "Train",
        "prompt": "write ok",
        "sandbox": {"input.txt": "fixture"},
        "grader": {"type": "contains", "file": "out.txt", "needle": "ok", "expected": "ok"},
    }
    val = dict(task, id="val-one", split="val", title="Val")
    tasks_mod.save(ws, [task, val])
    tasks_mod.materialize_all(ws, [task, val])
    wiki.ensure(ws)
    gating.ensure_active_repo(ws)
    return ws


class TaskRunner:
    def __init__(self, val_pass=None):
        self.val_pass = val_pass or {}
        self.calls = []

    def __call__(self, ws, prompt, *, tag, workdir=None, dry_run=False, **kwargs):
        self.calls.append(tag)
        if dry_run:
            return {"cmd": ["fake"], "dry_run": True}
        if "/train/" in tag or "/val/" in tag:
            task_id = tag.split("/")[-1]
            split = tag.split("/")[-2]
            iteration = int(tag.split("/")[0].split("-")[1])
            ok = split == "train" or task_id in self.val_pass.get(iteration, set())
            target = os.path.join(workdir or ws, "out.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("ok" if ok else "wrong")
            return {"exit_code": 0, "duration_s": 0.01, "stdout_path": None,
                    "session_file": None}
        return {"exit_code": 0, "duration_s": 0.01, "stdout_path": None,
                "session_file": None}


def _core_proposal():
    return {
        "target": "core",
        "action": "patch",
        "source_id": "demo-core",
        "base_sha": "a" * 40,
        "edits": [{"file": "src/value.txt", "op": "replace",
                   "target": "old", "content": "new"}],
    }


def _wire_proposal(monkeypatch, proposal=None):
    monkeypatch.setattr(harness, "maintain_step", lambda *a, **k: {})
    monkeypatch.setattr(
        harness, "propose_step",
        lambda *a, **k: (proposal or _core_proposal(), {}),
    )


class FakeCoreDriver:
    target = "core"
    candidate_runtime_bound = True

    def __init__(self, pre_gates=None, operational=False):
        self.events = []
        self.context = object()
        self.pre_gate_results = pre_gates if pre_gates is not None else [
            {"gate": "static", "status": "pass"},
            {"gate": "build", "status": "pass"},
            {"gate": "regression", "status": "pass"},
            {"gate": "performance", "status": "not_configured"},
        ]
        self.operational = operational

    def validate(self, ws, proposal):
        self.events.append("validate")

    def prepare(self, ws, iteration, proposal=None):
        assert proposal is not None
        self.events.append("prepare")
        return self.context

    def apply(self, ws, proposal, context=None):
        assert context is self.context
        self.events.append("apply")
        return "patch core source:demo-core files=1"

    def diff(self, ws, context=None):
        assert context is self.context
        self.events.append("diff")
        return "diff --git a/src/value.txt b/src/value.txt"

    def pre_gates(self, ws, proposal, context=None):
        assert context is self.context
        self.events.append("pre_gates")
        if self.operational:
            raise core_adapter.CoreOperationalError("runner unavailable")
        return self.pre_gate_results

    def accept(self, ws, iteration, score, context=None):
        assert context is self.context
        assert gating.load_state(ws)["r_best"] < score
        self.events.append("accept")
        return {"accepted_sha": "b" * 40}

    def rollback(self, ws, context=None):
        assert context is self.context
        self.events.append("rollback")
        return {"removed": True}


def test_v02_driver_calls_remain_compatible(tmp_path):
    ws = str(tmp_path / "ws")
    prompt_driver = assets.resolve_driver("prompt")
    prompt_driver.prepare(ws, 1)
    proposal = {"target": "prompt", "action": "create", "name": "inference",
                "content": "keep compatibility"}
    prompt_driver.apply(ws, proposal)
    assert "keep compatibility" in assets.read_prompt_overlay(ws)
    prompt_driver.rollback(ws)


def test_core_driver_delegates_context_lifecycle(monkeypatch, tmp_path):
    ws = str(tmp_path / "ws")
    proposal = _core_proposal()
    context = object()
    calls = []
    monkeypatch.setattr(core_adapter, "validate_core_proposal",
                        lambda w, p: calls.append("validate") or p)
    monkeypatch.setattr(core_adapter, "begin_candidate",
                        lambda w, i, p: calls.append("prepare") or context)
    monkeypatch.setattr(core_adapter, "apply_candidate",
                        lambda c: calls.append("apply") or "desc")
    monkeypatch.setattr(core_adapter, "candidate_diff",
                        lambda c: calls.append("diff") or "DIFF")
    monkeypatch.setattr(core_adapter, "run_pre_gates",
                        lambda c: calls.append("pre_gates") or [{"gate": "static", "status": "pass"}])
    monkeypatch.setattr(core_adapter, "accept_candidate",
                        lambda c, i: calls.append("accept") or {"accepted_sha": "b" * 40})
    monkeypatch.setattr(core_adapter, "reject_candidate",
                        lambda c: calls.append("rollback") or {"removed": True})

    driver = assets.resolve_driver("core")
    driver.validate(ws, proposal)
    got = driver.prepare(ws, 1, proposal)
    assert got is context
    assert driver.apply(ws, proposal, context) == "desc"
    assert driver.diff(ws, context) == "DIFF"
    assert driver.pre_gates(ws, proposal, context) == [{"gate": "static", "status": "pass"}]
    assert driver.accept(ws, 1, 0.9, context)["accepted_sha"] == "b" * 40
    assert driver.rollback(ws, context)["removed"] is True
    assert calls == ["validate", "prepare", "apply", "diff", "pre_gates", "accept", "rollback"]


def test_engineering_gate_failure_skips_held_out_gate(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _wire_proposal(monkeypatch)
    val_id = "val-one"
    runner = TaskRunner(val_pass={0: set(), 1: {val_id}})
    driver = FakeCoreDriver(pre_gates=[
        {"gate": "static", "status": "pass"},
        {"gate": "build", "status": "fail", "summary": "compile failed"},
    ])
    real_resolve = assets.resolve_driver
    monkeypatch.setattr(assets, "resolve_driver",
                        lambda target: driver if target == "core" else real_resolve(target))

    state = harness.evolve(ws, iters=1, runner=runner, verbose=False)
    item = state["history"][-1]
    assert item["status"] == "rejected"
    assert item["r_val"] is None
    assert item["engineering"]["gates"][-1]["gate"] == "build"
    assert not any("iter-01/val/" in tag for tag in runner.calls)
    assert driver.events[-1] == "rollback"
    assert state["r_best"] == 0.0


def test_core_operational_error_is_not_learned_as_rejection(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _wire_proposal(monkeypatch)
    runner = TaskRunner(val_pass={0: set()})
    driver = FakeCoreDriver(operational=True)
    real_resolve = assets.resolve_driver
    monkeypatch.setattr(assets, "resolve_driver",
                        lambda target: driver if target == "core" else real_resolve(target))

    state = harness.evolve(ws, iters=1, runner=runner, verbose=False)
    item = state["history"][-1]
    assert item["status"] == "operational_error"
    assert item["accepted"] is False
    assert state["r_best"] == 0.0
    assert "rollback" in driver.events
    impact = open(os.path.join(ws, "wiki", "skill-impact.md"), encoding="utf-8").read()
    assert "OPERATIONAL_ERROR" in impact
    assert "runner unavailable" in impact


def test_core_fake_binding_flag_cannot_bypass_runtime_evidence(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _wire_proposal(monkeypatch)
    runner = TaskRunner(val_pass={0: set(), 1: {"val-one"}})
    driver = FakeCoreDriver()
    real_resolve = assets.resolve_driver
    monkeypatch.setattr(assets, "resolve_driver",
                        lambda target: driver if target == "core" else real_resolve(target))

    state = harness.evolve(ws, iters=1, runner=runner, verbose=False)
    item = state["history"][-1]
    assert item["status"] == "operational_error"
    assert item["accepted"] is False
    assert item["r_val"] is None
    assert "candidate runtime" in item["error"].lower()
    assert item["engineering"]["gates"][-1]["status"] == "not_configured"
    assert not any("iter-01/val/" in tag for tag in runner.calls)
    assert driver.events[-1] == "rollback"
    assert "accept" not in driver.events
    assert state["r_best"] == 0.0


def test_engineering_audit_block_is_rendered(tmp_path):
    ws = str(tmp_path)
    text = prompts.gate_outcome_entry(
        ws, 1, _core_proposal(), None, False, "DIFF", 0.4,
        status="operational_error", error="runner unavailable",
        engineering={"source_id": "demo-core", "base_sha": "a" * 40,
                     "gates": [{"gate": "build", "status": "pass"}]},
    )
    assert "OPERATIONAL_ERROR" in text
    assert "Engineering evidence" in text
    assert "demo-core" in text
