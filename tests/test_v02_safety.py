"""Safety regressions for V0.2 candidate mutation boundaries."""

import os

import pytest

from wikiskill import assets, gating, harness, tasks as tasks_mod, wiki


def test_skill_create_prevalidates_purpose_md_before_mutation(tmp_path):
    ws = str(tmp_path / "ws")
    driver = assets.resolve_driver("skill")
    driver.prepare(ws, 0)
    proposal = {
        "target": "skill",
        "action": "create",
        "name": "bad-purpose",
        "skill_md": "---\nname: bad-purpose\n---\nbody\n",
        "purpose_md": 123,
    }
    with pytest.raises(ValueError, match="purpose_md must be text"):
        driver.validate(ws, proposal)
    assert not os.path.exists(os.path.join(driver.root(ws), "bad-purpose"))


def test_harness_rolls_back_if_apply_fails_after_prepare(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    wiki.ensure(ws)
    gating.ensure_active_repo(ws)
    task = {
        "id": "t1",
        "split": "train",
        "title": "T",
        "prompt": "P",
        "sandbox": {},
        "grader": {"type": "contains", "file": "out.txt", "needle": "ok"},
    }
    val = dict(task, id="v1", split="val")
    tasks_mod.save(ws, [task, val])
    gating.save_state(ws, {
        "domain": "ws",
        "baseline": 0.0,
        "r_best": 0.0,
        "next_iter": 1,
        "history": [],
    })

    monkeypatch.setattr(gating, "run_task", lambda ws, task, it, **kw: {**task, "score": 0.0})
    monkeypatch.setattr(harness, "maintain_step", lambda *a, **k: {})
    monkeypatch.setattr(
        harness,
        "propose_step",
        lambda *a, **k: ({
            "target": "prompt",
            "action": "create",
            "name": "inference",
            "content": "candidate",
        }, {}),
    )

    class ExplodingDriver:
        target = "prompt"

        def __init__(self):
            self.rolled_back = False
            self.partial = os.path.join(ws, "assets", "prompts", "active", "partial.txt")

        def validate(self, ws, proposal):
            return None

        def prepare(self, ws, iteration):
            os.makedirs(os.path.dirname(self.partial), exist_ok=True)

        def apply(self, ws, proposal):
            with open(self.partial, "w", encoding="utf-8") as f:
                f.write("partial")
            raise ValueError("apply exploded")

        def diff(self, ws):
            return ""

        def accept(self, ws, iteration, score):
            raise AssertionError("invalid candidate must not be accepted")

        def rollback(self, ws):
            self.rolled_back = True
            if os.path.exists(self.partial):
                os.remove(self.partial)

    exploding = ExplodingDriver()
    real_resolve = assets.resolve_driver
    monkeypatch.setattr(
        assets,
        "resolve_driver",
        lambda target: exploding if target == "prompt" else real_resolve(target),
    )

    state = harness.evolve(ws, iters=1, verbose=False, runner=lambda *a, **k: {})
    assert state["history"][-1]["status"] == "invalid"
    assert exploding.rolled_back is True
    assert not os.path.exists(exploding.partial)
