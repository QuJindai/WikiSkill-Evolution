"""Regression contract: Core held-out scoring must execute the candidate runtime."""

import os

from wikiskill import assets, gating, harness, tasks as tasks_mod, wiki


PROPOSAL = {
    "target": "core",
    "action": "patch",
    "source_id": "demo-core",
    "base_sha": "a" * 40,
    "edits": [
        {
            "file": "src/value.txt",
            "op": "replace",
            "target": "old",
            "content": "new",
        }
    ],
}


class BoundlessCoreDriver:
    target = "core"
    candidate_runtime_bound = False

    def __init__(self):
        self.context = object()
        self.events = []

    def validate(self, ws, proposal):
        self.events.append("validate")

    def prepare(self, ws, iteration, proposal=None):
        self.events.append("prepare")
        return self.context

    def apply(self, ws, proposal, context=None):
        self.events.append("apply")
        return "patch core source:demo-core files=1"

    def diff(self, ws, context=None):
        self.events.append("diff")
        return "diff --git a/src/value.txt b/src/value.txt"

    def pre_gates(self, ws, proposal, context=None):
        self.events.append("pre_gates")
        return [
            {"gate": "static", "status": "pass"},
            {"gate": "build", "status": "pass"},
            {"gate": "regression", "status": "pass"},
            {"gate": "performance", "status": "not_configured"},
        ]

    def accept(self, ws, iteration, score, context=None):
        self.events.append("accept")
        return {"accepted_sha": "b" * 40}

    def rollback(self, ws, context=None):
        self.events.append("rollback")
        return {"removed": True}


def _workspace(tmp_path):
    ws = str(tmp_path / "ws")
    harness.init_workspace(ws)
    train = {
        "id": "train-one",
        "split": "train",
        "title": "Train",
        "prompt": "write ok",
        "sandbox": {"input.txt": "fixture"},
        "grader": {"type": "contains", "file": "out.txt", "needle": "ok"},
    }
    val = dict(train, id="val-one", split="val", title="Val")
    tasks_mod.save(ws, [train, val])
    tasks_mod.materialize_all(ws, [train, val])
    wiki.ensure(ws)
    gating.ensure_active_repo(ws)
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
    return ws


def test_unbound_core_runtime_cannot_reach_held_out_acceptance(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    calls = []

    def runner(got_ws, prompt, *, tag, workdir=None, **kwargs):
        calls.append(tag)
        if tag.startswith("iter-"):
            with open(os.path.join(workdir, "out.txt"), "w", encoding="utf-8") as f:
                f.write("ok")
        return {
            "exit_code": 0,
            "duration_s": 0.01,
            "stdout_path": None,
            "session_file": None,
        }

    monkeypatch.setattr(harness, "maintain_step", lambda *a, **k: {})
    monkeypatch.setattr(harness, "propose_step", lambda *a, **k: (PROPOSAL, {}))
    driver = BoundlessCoreDriver()
    real_resolve = assets.resolve_driver
    monkeypatch.setattr(
        assets,
        "resolve_driver",
        lambda target: driver if target == "core" else real_resolve(target),
    )

    state = harness.evolve(ws, iters=1, runner=runner, verbose=False)
    item = state["history"][-1]

    assert item["status"] == "operational_error"
    assert item["accepted"] is False
    assert item["r_val"] is None
    assert "candidate runtime" in item["error"].lower()
    assert not any("iter-01/val/" in tag for tag in calls)
    assert driver.events[-1] == "rollback"
    assert "accept" not in driver.events
    assert state["r_best"] == 0.0
