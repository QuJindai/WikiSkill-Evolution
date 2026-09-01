"""Prompts for the three agent roles.

The Wiki Maintainer and Skill Proposer system prompts are adapted from the
paper's Appendix E (extracted verbatim from the arXiv HTML). The inference
prompt follows the paper's per-benchmark "You are an agent solving X" style.
"""

from __future__ import annotations

import json
import os

from . import assets

INFERENCE_PREFIX = (
    "You are an agent completing a task in a sandbox directory. "
    "Follow the instructions precisely. Use your tools to inspect files and "
    "verify your work before finishing. Write every deliverable file exactly "
    "as specified. Your final message should briefly state what you did.\n\n"
)


def inference_prompt(task: dict, sandbox: str | None = None,
                     ws: str | None = None) -> str:
    anchor = ""
    if sandbox:
        anchor = (
            f"\n\nWORKING DIRECTORY: {sandbox}\n"
            "Read input files from and write ALL deliverables into the WORKING "
            "DIRECTORY above. Use absolute paths (prefix every path with it). "
            "Do not guess the directory — it is given. Do NOT explore, read, or "
            "modify anything outside the WORKING DIRECTORY (ignore git state and "
            "files above it). Verify the final file exists at its absolute path "
            "before finishing.\n"
        )
    prefix = INFERENCE_PREFIX
    overlay = assets.read_prompt_overlay(ws) if ws else ""
    if overlay:
        prefix += (
            "WORKSPACE INFERENCE OVERLAY:\n"
            f"{overlay.rstrip()}\n\n"
        )
    return prefix + f"TASK: {task['title']}\n\n{task['prompt']}" + anchor


def _trace_manifest(traces: list[dict]) -> str:
    lines = []
    for t in traces:
        meta = t["meta"]
        lines.append(
            f"- {meta['task_id']} (split={t['split']}, iter={t['it']}, "
            f"score={meta.get('score')}): {meta.get('title', '')}\n"
            f"  trace: {t['path']}"
        )
    return "\n".join(lines)


def maintainer_prompt(ws: str, it: int, sampled_traces: list[dict]) -> str:
    wiki = os.path.join(ws, "wiki")
    manifest = "\n".join(f"- {t['path']}" for t in sampled_traces)
    return f"""You are the Wiki Maintainer in a WikiSkill evolution loop (iteration {it}).

Load the `wikiskill-maintainer` skill and follow it exactly.

WORKSPACE: {ws}
- Wiki (read + edit): {wiki}/  (index.md, log.md, skill-impact.md, patterns/)
- Raw traces to analyze this iteration (read only, immutable):

{manifest}

Procedure:
1. Read the trace JSONL files above (the agent's full step-by-step execution:
   reasoning, tool calls, outputs, final answer). Focus on the low-scoring
   traces for root-cause analysis; also read passing traces to preserve
   effective behavior.
   IMPORTANT — traces can be very long. Do NOT read every line of every trace.
   Use search_files to locate failures/errors, read the last few messages of a
   failed run, and skip tool outputs you don't need. Reading selectively leaves
   budget for writing.
2. Perform deep trace analysis: compare successful vs failed tasks, identify
   ACTION patterns (not just error messages), check whether any active skill
   guidance was helpful.
3. Update the wiki incrementally — WRITE EARLY, edit later. Create/update
   pattern pages under wiki/patterns/, rewrite wiki/index.md (complete content,
   one line per pattern: [name](wiki/patterns/name.md): PROBLEM + ROOT CAUSE +
   FIX in one or two sentences), append your findings to wiki/log.md.
   Finish writing before you run out of turns.
4. Keep pattern pages 10-30 lines, concise, no duplicates.
"""


def proposer_prompt(ws: str, it: int, train_results: list[dict]) -> str:
    wiki = os.path.join(ws, "wiki")
    rows = []
    for r in sorted(train_results, key=lambda x: (x.get("score") or 0, x["id"])):
        rows.append(f"- {r['id']} [{r['split']}] score={r.get('score')}: {r['title']}")
    table = "\n".join(rows)
    return f"""You are the Evolution Proposer in a WikiSkill evolution loop (iteration {it}).

Load the `wikiskill-proposer` skill and follow it exactly.

WORKSPACE: {ws}
- Wiki: {wiki}/ (index.md, skill-impact.md, patterns/, log.md)
- Raw traces: {os.path.join(ws, 'raw', 'traces', f'iter-{it:02d}')}/
- Active skills: {os.path.join(ws, 'skills', 'active')}/
- Prompt assets: {os.path.join(ws, 'assets', 'prompts', 'active')}/
- Harness assets: {os.path.join(ws, 'assets', 'harness', 'active')}/

Training rollout summary this iteration:
{table}

Rules:
1. Read wiki/index.md FIRST, then wiki/skill-impact.md. Do not repeat rejected
   or invalid approaches.
2. Read relevant pattern pages and at least 4 failed execution traces before
   proposing a mutation.
3. Choose exactly one target: skill, prompt, harness, core, or no_action.
4. The harness validates structure, applies the candidate through its Asset
   Driver, runs the same held-out gate, and accepts only if R_val > R_best.
5. Core mutation is unsupported in V0.2. Do not propose executable core
   changes; use no_action instead when only a core mutation would help.
6. Write exactly one JSON object to:
   {os.path.join(ws, 'runs', 'proposals', f'iter-{it:02d}.json')}

Proposal schemas:
Legacy skill compatibility:
{{"action": "create", "name": "skill_name", "skill_md": "...", "purpose_md": "..."}}

Skill:
{{"target": "skill", "action": "create", "name": "skill_name", "skill_md": "...", "purpose_md": "..."}}
{{"target": "skill", "action": "patch", "name": "skill_name", "edits": [{{"op": "append", "content": "..."}}]}}

Prompt overlay (V0.2 name is fixed to inference):
{{"target": "prompt", "action": "create", "name": "inference", "content": "..."}}
{{"target": "prompt", "action": "patch", "name": "inference", "edits": [{{"op": "replace", "target": "exact text", "content": "..."}}]}}

Harness policy (declarative only):
{{"target": "harness", "action": "create", "name": "policy", "policy": {{"inference_max_turns": 8}}}}
{{"target": "harness", "action": "patch", "name": "policy", "updates": {{"proposer_max_turns": 80}}}}
Allowed policy keys: inference_max_turns, maintainer_max_turns,
proposer_max_turns, maintainer_run_budget, proposer_run_budget.

Core contract:
{{"target": "core", "action": "patch", "name": "runtime", "manifest": {{"adapter": "none"}}}}
Core adapter mutation is unsupported in V0.2 and will be recorded as invalid
without running a validation rollout.

No action:
{{"action": "no_action"}}

Prefer the smallest transferable mutation supported by trace evidence. Keep
skills/prompts concise. Harness changes must use only allowed typed policy keys.
"""


def gate_outcome_entry(ws: str, it: int, proposal: dict, r_val: float | None,
                       accepted: bool, diff: str, baseline_r: float | None,
                       status: str | None = None, error: str | None = None,
                       engineering: dict | None = None) -> str:
    prop_path = os.path.join(ws, "runs", "proposals", f"iter-{it:02d}.json")
    target = proposal.get("target", "skill") if isinstance(proposal, dict) else "?"
    action = proposal.get("action", "?") if isinstance(proposal, dict) else "?"
    effective = status or ("accepted" if accepted else "rejected")
    label = {
        "accepted": "ACCEPTED",
        "rejected": "REJECTED",
        "invalid": "INVALID",
        "operational_error": "OPERATIONAL_ERROR",
        "no_action": "NO_ACTION",
    }.get(effective, effective.upper())
    head = (f"### iter-{it:02d} — {label} "
            f"(target={target}, R_val={r_val}, "
            f"R_best={'—' if baseline_r is None else baseline_r})")
    if effective == "no_action" or action == "no_action":
        return f"{head}\n\nProposal: no_action (no validation run performed).\n"

    name = proposal.get("name", "?") if isinstance(proposal, dict) else "?"
    body = [
        head, "",
        f"Target: `{target}`",
        f"Proposal: {action} `{name}`",
        f"Proposal file: `{prop_path}` (full content embedded below)",
    ]
    if error:
        body += ["", f"Validation error: {error}"]
    if engineering is not None:
        body += ["", "Engineering evidence:", "", "```json",
                 json.dumps(engineering, indent=2, sort_keys=True), "```"]
    if diff.strip():
        body += ["", "```diff", diff.strip(), "```"]
    body += [
        "",
        "Full proposal content (rejected/invalid proposals remain visible to future proposers):",
        "", "```json", json.dumps(proposal, indent=2), "```",
    ]
    if effective == "invalid":
        body += ["", "Validation: structural validation failed; no held-out rollout performed."]
    elif effective == "operational_error":
        body += ["", "Validation: operational failure; accepted source state and R_best were not advanced."]
    elif effective == "rejected" and r_val is None:
        body += ["", "Validation: engineering gate rejected the candidate before held-out rollout."]
    elif accepted:
        body += ["", f"Validation: {r_val} > R_best → accepted; candidate asset committed."]
    else:
        body += ["", f"Validation: {r_val} ≤ R_best → candidate asset rolled back; wiki retained."]
    return "\n".join(body)
