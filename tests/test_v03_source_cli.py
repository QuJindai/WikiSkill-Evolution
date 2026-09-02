"""V0.3 operator source CLI and Evolution Proposer contracts."""

import pytest

from wikiskill import cli, prompts, sources


def test_source_cli_routes_registered_source_operations(monkeypatch, tmp_path, capsys):
    ws = str(tmp_path / "ws")
    calls = []

    monkeypatch.setattr(
        sources,
        "register_source",
        lambda got_ws, manifest: calls.append(("register", got_ws, manifest))
        or {"source_id": "demo-core"},
    )
    monkeypatch.setattr(
        sources,
        "accepted_sha",
        lambda got_ws, source_id: "a" * 40,
    )
    monkeypatch.setattr(
        sources,
        "list_sources",
        lambda got_ws: calls.append(("list", got_ws))
        or [{
            "source_id": "demo-core",
            "adapter": "git_source",
            "accepted_sha": "a" * 40,
            "gates": {"static": "registered:wikiskill-static"},
        }],
    )
    monkeypatch.setattr(
        sources,
        "inspect_source",
        lambda got_ws, source_id: calls.append(("inspect", got_ws, source_id))
        or {"manifest": {"source_id": source_id},
            "state": {"accepted_sha": "a" * 40}},
    )
    monkeypatch.setattr(
        sources,
        "validate_registered_source",
        lambda got_ws, source_id: calls.append(("validate", got_ws, source_id))
        or {"status": "valid", "source_id": source_id,
            "accepted_sha": "a" * 40},
    )

    assert cli.main(["source", "register", "manifest.json", "--ws", ws]) == 0
    assert cli.main(["source", "list", "--ws", ws]) == 0
    assert cli.main(["source", "inspect", "demo-core", "--ws", ws]) == 0
    assert cli.main(["source", "validate", "demo-core", "--ws", ws]) == 0

    assert ("register", ws, "manifest.json") in calls
    assert ("list", ws) in calls
    assert ("inspect", ws, "demo-core") in calls
    assert ("validate", ws, "demo-core") in calls
    output = capsys.readouterr().out
    assert "demo-core" in output
    assert "valid" in output


def test_source_cli_defaults_workspace_to_current_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(sources, "list_sources", lambda ws: seen.append(ws) or [])
    assert cli.main(["source", "list"]) == 0
    assert seen == [str(tmp_path)]


def test_source_cli_has_no_arbitrary_exec(monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["source", "exec", "echo danger"])


def test_proposer_exposes_registered_sources_and_v03_core_schema(monkeypatch, tmp_path):
    ws = str(tmp_path)
    monkeypatch.setattr(
        sources,
        "proposer_source_summary",
        lambda got_ws: "- source_id: demo-core | accepted_sha=" + "a" * 40,
    )
    text = prompts.proposer_prompt(ws, 1, [])

    assert "source_id: demo-core" in text
    assert "accepted_sha=" + "a" * 40 in text
    assert '"target": "core"' in text
    assert '"action": "patch"' in text
    assert '"source_id"' in text
    assert '"base_sha"' in text
    assert '"file"' in text
    assert '"op"' in text
    assert '"target"' in text
    assert '"content"' in text
    assert "Core mutation is unsupported in V0.2" not in text
    assert "repository" in text.lower()
    assert "shell" in text.lower()
    assert "trusted registry" in text.lower()
