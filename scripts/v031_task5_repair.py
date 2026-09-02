from pathlib import Path
import runpy

# Apply the already-diagnosed Task 5 production implementation first.
runpy.run_path("scripts/v031_task5_green.py", run_name="__main__")

# V0.3.1 deliberately removes the old boolean candidate_runtime_bound shortcut.
# Update the obsolete V0.3 fake-driver contract to prove that the shortcut cannot
# bypass exact BoundRuntimeSession evidence. Real Core acceptance is covered by
# tests/test_v031_runtime_integration.py with real Git + Python runtime execution.
path = Path("tests/test_v03_contract.py")
text = path.read_text(encoding="utf-8")
old = '''def test_core_strict_improvement_accepts_after_pre_gates(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _wire_proposal(monkeypatch)
    runner = TaskRunner(val_pass={0: set(), 1: {"val-one"}})
    driver = FakeCoreDriver()
    real_resolve = assets.resolve_driver
    monkeypatch.setattr(assets, "resolve_driver",
                        lambda target: driver if target == "core" else real_resolve(target))

    state = harness.evolve(ws, iters=1, runner=runner, verbose=False)
    item = state["history"][-1]
    assert item["status"] == "accepted"
    assert item["accepted"] is True
    assert item["engineering"]["gates"][-1]["status"] == "not_configured"
    assert driver.events == ["validate", "prepare", "apply", "diff", "pre_gates", "accept"]
    assert state["r_best"] == 1.0
'''
new = '''def test_core_fake_binding_flag_cannot_bypass_runtime_evidence(monkeypatch, tmp_path):
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
'''
if old not in text:
    if "def test_core_fake_binding_flag_cannot_bypass_runtime_evidence" not in text:
        raise SystemExit("obsolete V0.3 fake-binding test anchor not found")
else:
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
