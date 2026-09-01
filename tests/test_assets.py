"""Asset-driver unit tests for WikiSkill-Evolution V0.2."""

import os

import pytest

from wikiskill import assets, gating, prompts


def test_legacy_proposal_defaults_to_skill():
    p = assets.normalize_proposal({
        "action": "create",
        "name": "x",
        "skill_md": "body",
    })
    assert p["target"] == "skill"
    assert p["action"] == "create"


def test_unknown_target_fails_closed():
    with pytest.raises(ValueError, match="unknown asset target"):
        assets.resolve_driver("repo_python")


def test_skill_driver_keeps_legacy_active_dir(tmp_path):
    ws = str(tmp_path / "ws")
    d = assets.resolve_driver("skill")
    d.prepare(ws, 1)
    assert d.root(ws) == gating.active_dir(ws)
    assert d.root(ws).endswith(os.path.join("skills", "active"))


def test_prompt_create_changes_only_workspace_prompt(tmp_path):
    ws = str(tmp_path / "ws")
    d = assets.resolve_driver("prompt")
    d.prepare(ws, 1)
    proposal = {
        "target": "prompt",
        "action": "create",
        "name": "inference",
        "content": "Always verify numeric units twice.",
    }
    d.validate(ws, proposal)
    d.apply(ws, proposal)
    task = {"title": "T", "prompt": "P"}
    out = prompts.inference_prompt(task, ws=ws)
    assert "Always verify numeric units twice." in out
    assert "Always verify numeric units twice." not in prompts.inference_prompt(task)


def test_prompt_reject_rollback_restores_exact_bytes(tmp_path):
    ws = str(tmp_path / "ws")
    d = assets.resolve_driver("prompt")
    d.prepare(ws, 1)
    path = assets.prompt_overlay_path(ws)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("BASE\n")
    d.prepare(ws, 2)
    proposal = {
        "target": "prompt",
        "action": "patch",
        "name": "inference",
        "edits": [{"op": "append", "content": "CANDIDATE"}],
    }
    d.validate(ws, proposal)
    d.apply(ws, proposal)
    assert open(path, encoding="utf-8").read() != "BASE\n"
    d.rollback(ws)
    assert open(path, encoding="utf-8").read() == "BASE\n"


def test_prompt_safe_path_rejects_traversal_and_absolute_path(tmp_path):
    root = str(tmp_path / "root")
    with pytest.raises(ValueError, match="unsafe asset path"):
        assets.safe_asset_path(root, "../outside.txt")
    with pytest.raises(ValueError, match="unsafe asset path"):
        assets.safe_asset_path(root, "/tmp/outside.txt")


def test_harness_policy_validation_and_roundtrip(tmp_path):
    ws = str(tmp_path / "ws")
    d = assets.resolve_driver("harness")
    d.prepare(ws, 1)
    proposal = {
        "target": "harness",
        "action": "create",
        "name": "policy",
        "policy": {
            "inference_max_turns": 7,
            "maintainer_max_turns": 70,
            "proposer_run_budget": 2400,
        },
    }
    d.validate(ws, proposal)
    d.apply(ws, proposal)
    assert assets.read_harness_policy(ws) == proposal["policy"]


def test_harness_policy_rejects_unknown_bool_and_out_of_range(tmp_path):
    ws = str(tmp_path / "ws")
    d = assets.resolve_driver("harness")
    d.prepare(ws, 1)
    bad = [
        {"unknown": 1},
        {"inference_max_turns": True},
        {"inference_max_turns": 0},
        {"inference_max_turns": 501},
    ]
    for policy in bad:
        with pytest.raises(ValueError):
            d.validate(ws, {
                "target": "harness",
                "action": "create",
                "name": "policy",
                "policy": policy,
            })


def test_harness_reject_rollback_restores_exact_policy(tmp_path):
    ws = str(tmp_path / "ws")
    d = assets.resolve_driver("harness")
    d.prepare(ws, 1)
    base = {
        "target": "harness",
        "action": "create",
        "name": "policy",
        "policy": {"inference_max_turns": 9},
    }
    d.validate(ws, base)
    d.apply(ws, base)
    d.accept(ws, 1, 0.5)
    d.prepare(ws, 2)
    patch = {
        "target": "harness",
        "action": "patch",
        "name": "policy",
        "updates": {"inference_max_turns": 13},
    }
    d.validate(ws, patch)
    d.apply(ws, patch)
    assert assets.read_harness_policy(ws)["inference_max_turns"] == 13
    d.rollback(ws)
    assert assets.read_harness_policy(ws) == {"inference_max_turns": 9}


def test_core_target_is_known_but_mutation_is_unsupported(tmp_path):
    d = assets.resolve_driver("core")
    assert d.target == "core"
    with pytest.raises(ValueError, match="core adapter.*unsupported"):
        d.validate(str(tmp_path), {
            "target": "core",
            "action": "patch",
            "name": "runtime",
            "manifest": {"adapter": "llama_cpp"},
        })
