"""Release-contract tests for WikiSkill-Evolution V0.2."""

from wikiskill import bench, cli, harness, prompts, tasks as tasks_mod


def _workspace(tmp_path):
    ws = str(tmp_path / "ws")
    harness.init_workspace(ws)
    tasks = bench.generate(42)
    tasks_mod.save(ws, tasks)
    tasks_mod.materialize_all(ws, tasks)
    return ws


def test_proposer_prompt_exposes_all_v02_asset_targets(tmp_path):
    ws = str(tmp_path)
    text = prompts.proposer_prompt(ws, 1, [])
    for token in ('"target": "skill"', '"target": "prompt"',
                  '"target": "harness"', '"target": "core"'):
        assert token in text
    assert 'name": "inference"' in text
    assert "inference_max_turns" in text
    assert "core" in text.lower() and "unsupported" in text.lower()


def test_cli_evolve_default_does_not_override_harness_policy(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    captured = []

    def fake_evolve(*args, **kwargs):
        captured.append(kwargs)
        return {"baseline": 0.0, "r_best": 0.0}

    monkeypatch.setattr(cli.harness, "evolve", fake_evolve)
    assert cli.main(["evolve", "demo", "--ws", ws]) == 0
    assert captured[-1]["max_turns"] is None

    assert cli.main(["evolve", "demo", "--ws", ws, "--max-turns", "21"]) == 0
    assert captured[-1]["max_turns"] == 21
