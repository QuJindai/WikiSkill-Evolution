"""Algorithm 1 orchestration with V0.2 governed asset candidates.

The Wiki loop remains unchanged: raw traces -> persistent wiki -> proposal ->
held-out gate. V0.2 delegates candidate mutation to an asset driver so skill,
prompt, and harness assets share one acceptance rule while core remains
fail-closed.
"""

from __future__ import annotations

import json
import os
import random
import shutil

from . import agents, assets, gating, prompts, tasks as tasks_mod, traces, wiki
from .backends.hermes import MAINTAINER_TOOLSETS, PROPOSER_TOOLSETS

FRAMEWORK_SKILLS = ("wikiskill-maintainer", "wikiskill-proposer")


def repo_skills_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "skills")


def init_workspace(ws: str) -> None:
    for d in ("raw/traces", "wiki/patterns", "skills/active", "skills/framework",
              "runs/proposals", "bench"):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    gating.ensure_active_repo(ws)
    wiki.ensure(ws)
    agents.bootstrap_profile(ws)
    fw = os.path.join(ws, "skills", "framework")
    for name in FRAMEWORK_SKILLS:
        src = os.path.join(repo_skills_dir(), name)
        dst = os.path.join(fw, name)
        if os.path.isdir(src) and not os.path.isdir(dst):
            shutil.copytree(src, dst)


def sample_traces(ws: str, it: int, train_results: list[dict],
                  max_total: int = 8, max_fail: int = 5, max_pass: int = 3) -> list[dict]:
    fails = [r for r in train_results if (r.get("score") or 0) < 1.0]
    passes = [r for r in train_results if (r.get("score") or 0) == 1.0]
    rng = random.Random(it)
    picked = (rng.sample(fails, min(max_fail, len(fails))) +
              rng.sample(passes, min(max_pass, len(passes))))
    out = []
    for r in picked:
        p = traces.transcript_path(ws, it, "train", r["id"])
        out.append({"task_id": r["id"], "score": r.get("score"),
                    "path": p if os.path.exists(p) else traces.meta_path(ws, it, "train", r["id"])})
    return out


def maintain_step(ws: str, k: int, sampled: list[dict], runner=agents.run_agent,
                  dry_run: bool = False) -> dict:
    prompt = prompts.maintainer_prompt(ws, k, sampled)
    max_turns = assets.resolve_harness_value(ws, "maintainer_max_turns", 60)
    run_budget = assets.resolve_harness_value(ws, "maintainer_run_budget", 1500)
    return runner(ws, prompt, tag=f"maintain-{k:02d}",
                  toolsets=MAINTAINER_TOOLSETS, include_framework=True,
                  dry_run=dry_run, max_turns=max_turns, run_budget=run_budget)


def propose_step(ws: str, k: int, train_results: list[dict], runner=agents.run_agent,
                 dry_run: bool = False) -> tuple[dict | None, dict]:
    prompt = prompts.proposer_prompt(ws, k, train_results)
    max_turns = assets.resolve_harness_value(ws, "proposer_max_turns", 60)
    run_budget = assets.resolve_harness_value(ws, "proposer_run_budget", 1800)
    res = runner(ws, prompt, tag=f"propose-{k:02d}",
                 toolsets=PROPOSER_TOOLSETS, include_framework=True,
                 dry_run=dry_run, max_turns=max_turns, run_budget=run_budget)
    ppath = os.path.join(ws, "runs", "proposals", f"iter-{k:02d}.json")
    if dry_run or not os.path.exists(ppath):
        return None, res
    with open(ppath, encoding="utf-8") as f:
        proposal = json.load(f)
    return proposal, res


def _history_entry(k: int, train_mean: float, *, target: str, action: str,
                   status: str, accepted: bool, proposal: str,
                   r_val: float | None = None, error: str | None = None) -> dict:
    out = {"iter": k, "train_mean": train_mean, "r_val": r_val,
           "accepted": accepted, "proposal": proposal, "target": target,
           "action": action, "status": status}
    if error:
        out["error"] = error
    return out


def evolve(ws: str, iters: int = 3, model: str | None = None,
           provider: str | None = None, runner=agents.run_agent,
           dry_run: bool = False, verbose: bool = True,
           max_turns: int | None = None, no_early_stop: bool = False) -> dict:
    def log(msg: str) -> None:
        if verbose:
            print(f"[wikiskill] {msg}")

    splits = tasks_mod.splits(ws)
    train, val = splits["train"], splits["val"]
    state = gating.load_state(ws)
    if model and not dry_run:
        agents.patch_profile_model(ws, model, provider)
        log(f"profile default model → {model} (provider: {provider or 'unchanged'})")
        model = None
    wiki.ensure(ws)

    if state.get("baseline") is None:
        log(f"baseline validation: {len(val)} val tasks, S0=∅")
        gate0 = gating.run_gate(ws, val, 0, model=model, runner=runner,
                                dry_run=dry_run, overwrite=True, max_turns=max_turns)
        state["baseline"] = gate0["mean"]
        state["r_best"] = gate0["mean"]
        wiki.append_log(ws, f"iter-00 baseline: R={gate0['mean']} (S0=∅, {len(val)} val tasks)")
        gating.save_state(ws, state)

    for k in range(state["next_iter"], iters + 1):
        if state["r_best"] == 1.0 and not no_early_stop:
            log("R_best == 1.0 → early stop")
            break
        log(f"iter {k}/{iters}: train rollouts ({len(train)} tasks)")
        train_results = [gating.run_task(ws, t, k, model=model, runner=runner,
                                         dry_run=dry_run, overwrite=True,
                                         max_turns=max_turns) for t in train]
        train_mean = gating.mean_score(train_results)

        sampled = sample_traces(ws, k, train_results)
        log(f"iter {k}: wiki maintenance (sampled {len(sampled)} traces)")
        maintain_step(ws, k, sampled, runner=runner, dry_run=dry_run)
        wiki.commit(ws, f"iter-{k:02d}: maintainer updates")

        log(f"iter {k}: evolution proposal")
        proposal, _ = propose_step(ws, k, train_results, runner=runner, dry_run=dry_run)
        raw = proposal if proposal is not None else {"action": "no_action"}
        try:
            proposal = assets.normalize_proposal(raw)
        except ValueError as exc:
            target = raw.get("target", "skill") if isinstance(raw, dict) else "?"
            action = raw.get("action", "?") if isinstance(raw, dict) else "?"
            err = str(exc)
            state["history"].append(_history_entry(
                k, train_mean, target=target, action=action, status="invalid",
                accepted=False, proposal=f"invalid {target}", error=err))
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
                ws, k, raw if isinstance(raw, dict) else {"raw": raw}, None,
                False, "", state["r_best"], status="invalid", error=err))
            wiki.append_log(ws, f"iter-{k:02d}: train={train_mean} proposal=invalid target={target}: {err}")
            wiki.commit(ws, f"iter-{k:02d}: invalid proposal")
            continue

        target = proposal["target"]
        action = proposal["action"]
        if action == "no_action":
            state["history"].append(_history_entry(
                k, train_mean, target=target, action=action, status="no_action",
                accepted=False, proposal="no_action"))
            wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
                ws, k, proposal, None, False, "", state["r_best"], status="no_action"))
            wiki.append_log(ws, f"iter-{k:02d}: train={train_mean} proposal=no_action")
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            wiki.commit(ws, f"iter-{k:02d}: no action")
            continue

        try:
            driver = assets.resolve_driver(target)
            driver.validate(ws, proposal)
            driver.prepare(ws, k)
            desc = driver.apply(ws, proposal)
            diff = driver.diff(ws)
        except ValueError as exc:
            err = str(exc)
            state["history"].append(_history_entry(
                k, train_mean, target=target, action=action, status="invalid",
                accepted=False, proposal=f"invalid {target}", error=err))
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
                ws, k, proposal, None, False, "", state["r_best"],
                status="invalid", error=err))
            wiki.append_log(ws, f"iter-{k:02d}: train={train_mean} proposal=invalid target={target}: {err}")
            wiki.commit(ws, f"iter-{k:02d}: invalid proposal")
            continue

        gatek = gating.run_gate(ws, val, k, model=model, runner=runner,
                                dry_run=dry_run, overwrite=True, max_turns=max_turns)
        r_val = gatek["mean"]
        prev_best = state["r_best"]
        accepted = r_val > prev_best
        if accepted:
            driver.accept(ws, k, r_val)
            state["r_best"] = r_val
            status = "accepted"
        else:
            driver.rollback(ws)
            status = "rejected"
        state["history"].append(_history_entry(
            k, train_mean, target=target, action=action, status=status,
            accepted=accepted, proposal=desc, r_val=r_val))
        state["next_iter"] = k + 1
        gating.save_state(ws, state)
        wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
            ws, k, proposal, r_val, accepted, diff, prev_best, status=status))
        wiki.append_log(
            ws, f"iter-{k:02d}: train={train_mean} target={target} propose={desc} "
                f"R_val={r_val} {'ACCEPT' if accepted else 'reject'} (R_best={state['r_best']})")
        wiki.commit(ws, f"iter-{k:02d}: gate outcome "
                        f"({'accept' if accepted else 'reject'}, target={target}, R_val={r_val})")

    return state
