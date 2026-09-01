"""Gating: validation runs, strict-improvement acceptance, candidate state,
and persisted evolution history.

V0.2 keeps the original skill lifecycle API as compatibility wrappers around
the Skill Asset Driver. Scoring semantics are unchanged:
  if R(T_val,k) > R_best: accept
  else: reject and roll back candidate asset; wiki retained.
"""

from __future__ import annotations

import json
import os
import subprocess

from . import agents, assets, prompts, scoring, traces

STATE_FILE = os.path.join("runs", "state.json")


def state_path(ws: str) -> str:
    return os.path.join(ws, STATE_FILE)


def load_state(ws: str) -> dict:
    p = state_path(ws)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"domain": os.path.basename(ws.rstrip("/")), "r_best": None,
            "baseline": None, "next_iter": 1, "history": []}


def save_state(ws: str, state: dict) -> None:
    os.makedirs(os.path.dirname(state_path(ws)), exist_ok=True)
    with open(state_path(ws), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------- legacy skill wrappers

def active_dir(ws: str) -> str:
    return assets.resolve_driver("skill").root(ws)


def ensure_active_repo(ws: str) -> None:
    assets.resolve_driver("skill").prepare(ws, 0)


def git_active(ws: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=active_dir(ws),
                          capture_output=True, text=True, check=check)


def commit_base(ws: str, k: int) -> None:
    assets.resolve_driver("skill").prepare(ws, k)


def apply_proposal(ws: str, proposal: dict) -> str:
    return assets.resolve_driver("skill").apply(ws, proposal)


def rollback(ws: str) -> None:
    assets.resolve_driver("skill").rollback(ws)


def skill_diff(ws: str) -> str:
    return assets.resolve_driver("skill").diff(ws)


def accept_commit(ws: str, k: int, r_val: float) -> None:
    assets.resolve_driver("skill").accept(ws, k, r_val)


# ---------------------------------------------------------------- run helpers

def _session_had_no_tool_calls(session_jsonl: str) -> bool:
    """True when the exported session performed zero tool calls (launch failure)."""
    try:
        with open(session_jsonl, encoding="utf-8", errors="replace") as f:
            first = json.loads(f.readline())
        return first.get("tool_call_count") == 0
    except (OSError, ValueError):
        return False


def run_task(ws: str, task: dict, it: int, *, model: str | None = None,
             runner=agents.run_agent, dry_run: bool = False,
             overwrite: bool = False) -> dict:
    """Inference rollout on one task + grading + trace capture."""
    from . import tasks as tasks_mod

    sandbox = tasks_mod.sandbox_dir(ws, task["id"])
    if not os.path.isdir(sandbox):
        sandbox = tasks_mod.materialize(ws, task)
    else:
        # Fresh execution environment per rollout: never grade stale
        # deliverables left by earlier runs (a dead agent run would otherwise
        # score phantom values from the previous experiment).
        tasks_mod.materialize(ws, task, force=True)
    tag = f"iter-{it:02d}/{task['split']}/{task['id']}"
    prompt = prompts.inference_prompt(task, sandbox=sandbox, ws=ws)
    res = runner(ws, prompt, tag=tag, workdir=sandbox, model=model, dry_run=dry_run)
    score = None if dry_run else scoring.grade(task, sandbox)
    if not dry_run and res.get("session_file") and _session_had_no_tool_calls(res["session_file"]):
        print(f"[wikiskill] ⚠ task {task['id']} session made 0 tool calls "
              f"(agent launch failure?) — scored from fresh sandbox, expect 0.0")
    meta = {
        "task_id": task["id"], "split": task["split"], "iter": it,
        "title": task["title"], "score": score, "model": model,
        "exit_code": res.get("exit_code"), "duration_s": res.get("duration_s"),
    }
    traces.save_trace(ws, it, task["split"], task["id"], meta,
                      transcript_src=res.get("session_file"),
                      overwrite=overwrite or bool(dry_run))
    return {**task, "score": score, "result": res}


def mean_score(results: list[dict]) -> float:
    scores = [r["score"] for r in results if r["score"] is not None]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 4)


def run_gate(ws: str, tasks: list[dict], it: int, *, model: str | None = None,
             runner=agents.run_agent, dry_run: bool = False,
             overwrite: bool = False) -> dict:
    """Validation rollout over a task split with the current active assets."""
    results = [run_task(ws, t, it, model=model, runner=runner, dry_run=dry_run,
                        overwrite=overwrite)
               for t in tasks]
    return {"iter": it, "results": results, "mean": mean_score(results)}
