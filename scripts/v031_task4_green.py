from pathlib import Path

# ---------------- gating.py ----------------
path = Path('wikiskill/gating.py')
text = path.read_text()
text = text.replace('import subprocess\n', 'import subprocess\nimport tempfile\n', 1)
old = '''def save_state(ws: str, state: dict) -> None:\n    os.makedirs(os.path.dirname(state_path(ws)), exist_ok=True)\n    with open(state_path(ws), "w", encoding="utf-8") as f:\n        json.dump(state, f, indent=2)\n'''
new = '''def save_state(ws: str, state: dict) -> None:\n    path = state_path(ws)\n    parent = os.path.dirname(path)\n    os.makedirs(parent, exist_ok=True)\n    fd, tmp = tempfile.mkstemp(\n        prefix=os.path.basename(path) + ".tmp-", dir=parent, text=True\n    )\n    try:\n        with os.fdopen(fd, "w", encoding="utf-8") as f:\n            json.dump(state, f, indent=2)\n            f.write("\\n")\n        os.replace(tmp, path)\n    finally:\n        if os.path.exists(tmp):\n            try:\n                os.remove(tmp)\n            except OSError:\n                pass\n'''
if old not in text:
    raise SystemExit('gating save_state anchor not found')
text = text.replace(old, new, 1)

anchor = '''def _session_had_no_tool_calls(session_jsonl: str) -> bool:\n'''
insert = '''RUNTIME_EVIDENCE_KEYS = (\n    "source_id", "source_sha", "binding_profile", "fingerprint"\n)\n\n\ndef _bounded_runtime_evidence(value: dict | None) -> dict | None:\n    if value is None:\n        return None\n    if not isinstance(value, dict):\n        raise ValueError("runtime evidence must be an object")\n    missing = [key for key in RUNTIME_EVIDENCE_KEYS if key not in value]\n    if missing:\n        raise ValueError(f"runtime evidence missing keys: {missing}")\n    out = {}\n    for key in RUNTIME_EVIDENCE_KEYS:\n        item = value[key]\n        if not isinstance(item, str) or not item:\n            raise ValueError(f"runtime evidence {key} must be non-empty text")\n        out[key] = item\n    return out\n\n\n'''
if anchor not in text:
    raise SystemExit('gating session helper anchor not found')
text = text.replace(anchor, insert + anchor, 1)

old = '''def run_task(ws: str, task: dict, it: int, *, model: str | None = None,\n             runner=agents.run_agent, dry_run: bool = False,\n             overwrite: bool = False, max_turns: int | None = None) -> dict:\n'''
new = '''def run_task(ws: str, task: dict, it: int, *, model: str | None = None,\n             runner=agents.run_agent, dry_run: bool = False,\n             overwrite: bool = False, max_turns: int | None = None,\n             runtime_evidence: dict | None = None) -> dict:\n'''
if old not in text:
    raise SystemExit('gating run_task signature anchor not found')
text = text.replace(old, new, 1)

old = '''    meta = {\n        "task_id": task["id"], "split": task["split"], "iter": it,\n        "title": task["title"], "score": score, "model": model,\n        "exit_code": res.get("exit_code"), "duration_s": res.get("duration_s"),\n    }\n    traces.save_trace(ws, it, task["split"], task["id"], meta,\n'''
new = '''    meta = {\n        "task_id": task["id"], "split": task["split"], "iter": it,\n        "title": task["title"], "score": score, "model": model,\n        "exit_code": res.get("exit_code"), "duration_s": res.get("duration_s"),\n    }\n    bounded_runtime = _bounded_runtime_evidence(runtime_evidence)\n    if bounded_runtime is not None:\n        meta["runtime"] = bounded_runtime\n    traces.save_trace(ws, it, task["split"], task["id"], meta,\n'''
if old not in text:
    raise SystemExit('gating meta anchor not found')
text = text.replace(old, new, 1)

old = '''def run_gate(ws: str, tasks: list[dict], it: int, *, model: str | None = None,\n             runner=agents.run_agent, dry_run: bool = False,\n             overwrite: bool = False, max_turns: int | None = None) -> dict:\n    """Validation rollout over a task split with the current active assets."""\n    results = [run_task(ws, t, it, model=model, runner=runner, dry_run=dry_run,\n                        overwrite=overwrite, max_turns=max_turns)\n               for t in tasks]\n    return {"iter": it, "results": results, "mean": mean_score(results)}\n'''
new = '''def run_gate(ws: str, tasks: list[dict], it: int, *, model: str | None = None,\n             runner=agents.run_agent, dry_run: bool = False,\n             overwrite: bool = False, max_turns: int | None = None,\n             runtime_evidence: dict | None = None) -> dict:\n    """Validation rollout over a task split with the current active assets."""\n    common = {\n        "model": model,\n        "runner": runner,\n        "dry_run": dry_run,\n        "overwrite": overwrite,\n        "max_turns": max_turns,\n    }\n    if runtime_evidence is not None:\n        common["runtime_evidence"] = runtime_evidence\n    results = [run_task(ws, t, it, **common) for t in tasks]\n    return {"iter": it, "results": results, "mean": mean_score(results)}\n'''
if old not in text:
    raise SystemExit('gating run_gate anchor not found')
text = text.replace(old, new, 1)
path.write_text(text)

# ---------------- cli.py ----------------
path = Path('wikiskill/cli.py')
text = path.read_text()
old = 'from . import agents, backends, bench, compare, gating, harness, sources, tasks as tasks_mod, traces, transfer\n'
new = '''from . import (\n    agents, backends, bench, compare, gating, harness, runtime_bindings, sources,\n    tasks as tasks_mod, traces, transfer,\n)\n'''
if old not in text:
    raise SystemExit('cli import anchor not found')
text = text.replace(old, new, 1)

start = text.index('def cmd_run_task(args) -> int:\n')
end = text.index('\ndef cmd_maintain(args) -> int:\n', start)
run_task_impl = '''def cmd_run_task(args) -> int:\n    ws = resolve_ws(args.domain, args.ws)\n    tasks = tasks_mod.load(ws)\n    t = next((t for t in tasks if t["id"] == args.task_id), None)\n    if not t:\n        print(f"unknown task: {args.task_id}")\n        return 1\n\n    bound = runtime_bindings.bind_active_accepted(ws)\n    if bound is not None:\n        try:\n            r = gating.run_task(\n                ws, t, args.iter, model=args.model, dry_run=args.dry_run,\n                max_turns=args.max_turns, runner=bound.runner,\n                runtime_evidence=bound.evidence,\n            )\n        finally:\n            bound.close()\n    else:\n        r = gating.run_task(\n            ws, t, args.iter, model=args.model, dry_run=args.dry_run,\n            max_turns=args.max_turns,\n            runner=lambda *a, **k: agents.run_agent(\n                *a, **k, run_budget=args.run_budget\n            ),\n        )\n    print(f"task {t['id']}: score={r.get('score')}")\n    if r.get("result", {}).get("cmd") and args.dry_run:\n        print("cmd: " + " ".join(r["result"]["cmd"]))\n    return 0\n\n'''
text = text[:start] + run_task_impl + text[end + 1:]

anchor = '''def cmd_source_validate(args) -> int:\n    ws = _source_ws(args)\n    result = sources.validate_registered_source(ws, args.source_id)\n    print(\n        f"{result['status']}: {result['source_id']} "\n        f"accepted_sha={result['accepted_sha']}"\n    )\n    return 0\n\n\n'''
addition = anchor + '''def _runtime_ws(args) -> str:\n    return os.path.abspath(args.ws) if args.ws else os.getcwd()\n\n\ndef cmd_runtime_bind(args) -> int:\n    ws = _runtime_ws(args)\n    result = runtime_bindings.bind_source(ws, args.source_id, args.binding_profile)\n    print(\n        f"runtime bound: {result['source_id']} "\n        f"profile={result['binding_profile']} role={result['role']}"\n    )\n    return 0\n\n\ndef cmd_runtime_activate(args) -> int:\n    ws = _runtime_ws(args)\n    result = runtime_bindings.activate_source(ws, args.source_id)\n    print(\n        f"runtime active: {result['source_id']} "\n        f"profile={result['binding_profile']} accepted_sha={result['accepted_sha']} "\n        f"fingerprint={result['fingerprint']}"\n    )\n    return 0\n\n\ndef cmd_runtime_inspect(args) -> int:\n    ws = _runtime_ws(args)\n    print(json.dumps(runtime_bindings.inspect_runtime(ws), indent=2, sort_keys=True))\n    return 0\n\n\ndef cmd_runtime_validate(args) -> int:\n    ws = _runtime_ws(args)\n    result = runtime_bindings.validate_runtime(ws)\n    print(json.dumps(result, indent=2, sort_keys=True))\n    return 0\n\n\n'''
if anchor not in text:
    raise SystemExit('cli source validate anchor not found')
text = text.replace(anchor, addition, 1)

anchor = '''    source_sp = source_sub.add_parser("validate", help="validate one registered source")\n    source_sp.add_argument("source_id")\n    source_sp.add_argument("--ws", help="workspace path (default: current directory)")\n    source_sp.set_defaults(fn=cmd_source_validate)\n\n'''
addition = anchor + '''    sp = sub.add_parser("runtime", help="trusted inference runtime operations")\n    runtime_sub = sp.add_subparsers(dest="runtime_cmd", required=True)\n\n    runtime_sp = runtime_sub.add_parser("bind", help="bind a source to a trusted runtime profile")\n    runtime_sp.add_argument("source_id")\n    runtime_sp.add_argument("binding_profile")\n    runtime_sp.add_argument("--ws", help="workspace path (default: current directory)")\n    runtime_sp.set_defaults(fn=cmd_runtime_bind)\n\n    runtime_sp = runtime_sub.add_parser("activate", help="activate a bound inference source")\n    runtime_sp.add_argument("source_id")\n    runtime_sp.add_argument("--ws", help="workspace path (default: current directory)")\n    runtime_sp.set_defaults(fn=cmd_runtime_activate)\n\n    runtime_sp = runtime_sub.add_parser("inspect", help="inspect trusted runtime state")\n    runtime_sp.add_argument("--ws", help="workspace path (default: current directory)")\n    runtime_sp.set_defaults(fn=cmd_runtime_inspect)\n\n    runtime_sp = runtime_sub.add_parser("validate", help="validate trusted runtime state")\n    runtime_sp.add_argument("--ws", help="workspace path (default: current directory)")\n    runtime_sp.set_defaults(fn=cmd_runtime_validate)\n\n'''
if anchor not in text:
    raise SystemExit('cli source parser anchor not found')
text = text.replace(anchor, addition, 1)
path.write_text(text)

# ---------------- prompts.py ----------------
path = Path('wikiskill/prompts.py')
text = path.read_text()
old = '''5. Core mutation is allowed only for a source_id listed in the trusted registry.\n   Use that source's current accepted_sha exactly as base_sha. Repository identity,\n   allow/deny policy, gate profiles, and accepted-ref authority come only from the\n   trusted registry. Never put repository URLs/paths, shell/command/script fields,\n   environment variables, credentials, or gate definitions in a proposal.\n6. Write exactly one JSON object to:\n'''
new = '''5. Core mutation is allowed only for a source_id listed in the trusted registry.\n   Use that source's current accepted_sha exactly as base_sha. Repository identity,\n   allow/deny policy, gate profiles, and accepted-ref authority come only from the\n   trusted registry. Never put repository URLs/paths, shell/command/script fields,\n   environment variables, credentials, or gate definitions in a proposal.\n6. Runtime binding is trusted operator-controlled state. A proposal MUST NOT\n   include binding_profile, runtime profile/entrypoint/argv/timeout/environment,\n   device routing, or any other runtime execution control.\n7. Write exactly one JSON object to:\n'''
if old not in text:
    raise SystemExit('prompts rules anchor not found')
text = text.replace(old, new, 1)
old = '''and base_sha must match the trusted registry summary above. The proposer cannot\nchange repository identity, source policy, shell commands, gate definitions, or\ncredentials.\n'''
new = '''and base_sha must match the trusted registry summary above. The proposer cannot\nchange repository identity, source policy, shell commands, gate definitions, or\ncredentials. It also cannot choose a runtime binding_profile or any runtime\nexecution setting; those are trusted operator-controlled state.\n'''
if old not in text:
    raise SystemExit('prompts core contract anchor not found')
text = text.replace(old, new, 1)
path.write_text(text)
