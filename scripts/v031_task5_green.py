from pathlib import Path

path = Path('wikiskill/harness.py')
text = path.read_text()
text = text.replace('import json\n', 'import copy\nimport json\n', 1)
old_import = 'from . import agents, assets, core_adapter, gating, prompts, tasks as tasks_mod, traces, wiki\n'
new_import = '''from . import (\n    agents, assets, core_adapter, gating, prompts, runtime_bindings,\n    tasks as tasks_mod, traces, wiki,\n)\n'''
if old_import not in text:
    raise SystemExit('harness import anchor not found')
text = text.replace(old_import, new_import, 1)

evolve_start = text.index('def evolve(\n')
prefix = text[:evolve_start]
helpers = r'''def _runtime_state_identity(active: dict | None) -> dict | None:
    if active is None:
        return None
    return {
        "source_id": active["source_id"],
        "binding_profile": active["binding_profile"],
        "accepted_sha": active["accepted_sha"],
        "fingerprint": active["fingerprint"],
    }


def _active_runtime_checked(ws: str, state: dict) -> dict | None:
    try:
        active = runtime_bindings.active_runtime_config(ws)
    except ValueError as exc:
        raise runtime_bindings.RuntimeBindingError(
            f"runtime configuration invalid: {exc}"
        ) from exc
    if state.get("baseline") is None:
        return active
    expected = _runtime_state_identity(active)
    recorded = state.get("runtime_identity")
    if expected != recorded:
        raise runtime_bindings.RuntimeBindingError(
            "runtime identity mismatch between scored state and authoritative "
            f"accepted runtime: recorded={recorded!r} active={expected!r}"
        )
    return active


def _bind_accepted_runtime(ws: str, state: dict):
    active = _active_runtime_checked(ws, state)
    try:
        bound = runtime_bindings.bind_active_accepted(ws)
    except ValueError as exc:
        raise runtime_bindings.RuntimeBindingError(
            f"active accepted runtime could not bind: {exc}"
        ) from exc
    if active is None:
        if bound is not None:
            try:
                bound.close()
            finally:
                raise runtime_bindings.RuntimeBindingError(
                    "runtime binding exists without active runtime configuration"
                )
        return None, None
    if bound is None:
        raise runtime_bindings.RuntimeBindingError(
            "active runtime configuration did not produce a bound runtime"
        )
    expected = _runtime_state_identity(active)
    if (
        bound.source_id != active["source_id"]
        or bound.source_sha != active["accepted_sha"]
        or bound.profile_id != active["binding_profile"]
        or bound.fingerprint != active["fingerprint"]
    ):
        try:
            bound.close()
        finally:
            raise runtime_bindings.RuntimeBindingError(
                "bound accepted runtime identity does not match active runtime"
            )
    return bound, expected


def _run_gate_with_accepted_runtime(
    ws: str,
    state: dict,
    tasks: list[dict],
    iteration: int,
    *,
    framework_runner,
    model: str | None,
    dry_run: bool,
    overwrite: bool,
    max_turns: int | None,
) -> tuple[dict, dict | None]:
    bound, identity = _bind_accepted_runtime(ws, state)
    if bound is None:
        return (
            gating.run_gate(
                ws,
                tasks,
                iteration,
                model=model,
                runner=framework_runner,
                dry_run=dry_run,
                overwrite=overwrite,
                max_turns=max_turns,
            ),
            None,
        )
    try:
        result = gating.run_gate(
            ws,
            tasks,
            iteration,
            model=model,
            runner=bound.runner,
            dry_run=dry_run,
            overwrite=overwrite,
            max_turns=max_turns,
            runtime_evidence=bound.evidence,
        )
    finally:
        bound.close()
    return result, identity


def _run_train_with_accepted_runtime(
    ws: str,
    state: dict,
    tasks: list[dict],
    iteration: int,
    *,
    framework_runner,
    model: str | None,
    dry_run: bool,
    max_turns: int | None,
) -> tuple[list[dict], dict | None]:
    bound, identity = _bind_accepted_runtime(ws, state)
    if bound is None:
        results = [
            gating.run_task(
                ws,
                task,
                iteration,
                model=model,
                runner=framework_runner,
                dry_run=dry_run,
                overwrite=True,
                max_turns=max_turns,
            )
            for task in tasks
        ]
        return results, None
    try:
        results = [
            gating.run_task(
                ws,
                task,
                iteration,
                model=model,
                runner=bound.runner,
                dry_run=dry_run,
                overwrite=True,
                max_turns=max_turns,
                runtime_evidence=bound.evidence,
            )
            for task in tasks
        ]
    finally:
        bound.close()
    return results, identity


def _audit_outcome_only(
    ws: str,
    state: dict,
    k: int,
    train_mean: float,
    proposal: dict,
    desc: str,
    diff: str,
    *,
    status: str,
    accepted: bool,
    r_val: float | None,
    prev_best: float | None,
    error: str | None = None,
    engineering: dict | None = None,
) -> None:
    target = proposal.get("target", "skill")
    wiki.append_skill_impact(
        ws,
        prompts.gate_outcome_entry(
            ws,
            k,
            proposal,
            r_val,
            accepted,
            diff,
            prev_best,
            status=status,
            error=error,
            engineering=engineering,
        ),
    )
    line = (
        f"iter-{k:02d}: train={train_mean} target={target} status={status} "
        f"R_val={r_val} R_best={state['r_best']}"
    )
    if error:
        line += f" error={error}"
    wiki.append_log(ws, line)
    wiki.commit(ws, f"iter-{k:02d}: gate outcome {status} target={target}")


def _append_state_outcome(
    state: dict,
    k: int,
    train_mean: float,
    proposal: dict,
    desc: str,
    *,
    status: str,
    accepted: bool,
    r_val: float | None,
    error: str | None = None,
    engineering: dict | None = None,
) -> None:
    state["history"].append(
        _history_entry(
            k,
            train_mean,
            target=proposal.get("target", "skill"),
            action=proposal.get("action", "?"),
            status=status,
            accepted=accepted,
            proposal=desc,
            r_val=r_val,
            error=error,
            engineering=engineering,
        )
    )
    state["next_iter"] = k + 1


def _core_runtime_failure(
    ws: str,
    state: dict,
    k: int,
    train_mean: float,
    proposal: dict,
    desc: str,
    diff: str,
    driver,
    context,
    pre_gates: list[dict],
    exc: Exception,
) -> None:
    cleanup = None
    error = str(exc)
    try:
        cleanup = _driver_rollback(driver, "core", ws, context)
    except Exception as rollback_exc:
        error += f"; rollback failed: {rollback_exc}"
    engineering = _engineering_evidence(
        proposal, context, pre_gates, cleanup=cleanup
    )
    _record_outcome(
        ws,
        state,
        k,
        train_mean,
        proposal,
        desc,
        diff,
        status="operational_error",
        accepted=False,
        r_val=None,
        prev_best=state["r_best"],
        error=error,
        engineering=engineering,
    )


def _driver_seal(driver, ws: str, iteration: int, context):
    return driver.seal(ws, iteration, context)


def _driver_advance_source(driver, ws: str, iteration: int, context):
    return driver.advance_source(ws, iteration, context)


def _driver_compensate_source(
    driver, ws: str, iteration: int, transition: dict, context
):
    return driver.compensate_source(ws, iteration, transition, context)


def _driver_release_candidate_ref(driver, ws: str, iteration: int, context):
    return driver.release_candidate_ref(ws, iteration, context)


'''

evolve = r'''def evolve(
    ws: str,
    iters: int = 3,
    model: str | None = None,
    provider: str | None = None,
    runner=agents.run_agent,
    dry_run: bool = False,
    verbose: bool = True,
    max_turns: int | None = None,
    no_early_stop: bool = False,
) -> dict:
    def log(message: str) -> None:
        if verbose:
            print(f"[wikiskill] {message}")

    splits = tasks_mod.splits(ws)
    train, val = splits["train"], splits["val"]
    state = gating.load_state(ws)
    if model and not dry_run:
        agents.patch_profile_model(ws, model, provider)
        log(f"profile default model → {model} (provider: {provider or 'unchanged'})")
        model = None
    wiki.ensure(ws)

    if state.get("baseline") is not None:
        _active_runtime_checked(ws, state)

    if state.get("baseline") is None:
        log(f"baseline validation: {len(val)} val tasks, S0=∅")
        gate0, runtime_identity = _run_gate_with_accepted_runtime(
            ws,
            state,
            val,
            0,
            framework_runner=runner,
            model=model,
            dry_run=dry_run,
            overwrite=True,
            max_turns=max_turns,
        )
        state["baseline"] = gate0["mean"]
        state["r_best"] = gate0["mean"]
        if runtime_identity is not None:
            state["runtime_identity"] = runtime_identity
        else:
            state.pop("runtime_identity", None)
        wiki.append_log(
            ws,
            f"iter-00 baseline: R={gate0['mean']} (S0=∅, {len(val)} val tasks)",
        )
        gating.save_state(ws, state)

    for k in range(state["next_iter"], iters + 1):
        _active_runtime_checked(ws, state)
        if state["r_best"] == 1.0 and not no_early_stop:
            log("R_best == 1.0 → early stop")
            break

        log(f"iter {k}/{iters}: train rollouts ({len(train)} tasks)")
        train_results, _ = _run_train_with_accepted_runtime(
            ws,
            state,
            train,
            k,
            framework_runner=runner,
            model=model,
            dry_run=dry_run,
            max_turns=max_turns,
        )
        train_mean = gating.mean_score(train_results)

        sampled = sample_traces(ws, k, train_results)
        log(f"iter {k}: wiki maintenance (sampled {len(sampled)} traces)")
        maintain_step(ws, k, sampled, runner=runner, dry_run=dry_run)
        wiki.commit(ws, f"iter-{k:02d}: maintainer updates")

        log(f"iter {k}: evolution proposal")
        proposal, _ = propose_step(
            ws, k, train_results, runner=runner, dry_run=dry_run
        )
        raw = proposal if proposal is not None else {"action": "no_action"}
        try:
            proposal = assets.normalize_proposal(raw)
        except ValueError as exc:
            target = raw.get("target", "skill") if isinstance(raw, dict) else "?"
            action = raw.get("action", "?") if isinstance(raw, dict) else "?"
            error = str(exc)
            state["history"].append(
                _history_entry(
                    k,
                    train_mean,
                    target=target,
                    action=action,
                    status="invalid",
                    accepted=False,
                    proposal=f"invalid {target}",
                    error=error,
                )
            )
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            wiki.append_skill_impact(
                ws,
                prompts.gate_outcome_entry(
                    ws,
                    k,
                    raw if isinstance(raw, dict) else {"raw": raw},
                    None,
                    False,
                    "",
                    state["r_best"],
                    status="invalid",
                    error=error,
                ),
            )
            wiki.append_log(
                ws,
                f"iter-{k:02d}: train={train_mean} proposal=invalid "
                f"target={target}: {error}",
            )
            wiki.commit(ws, f"iter-{k:02d}: invalid proposal")
            continue

        target = proposal["target"]
        action = proposal["action"]
        if action == "no_action":
            state["history"].append(
                _history_entry(
                    k,
                    train_mean,
                    target=target,
                    action=action,
                    status="no_action",
                    accepted=False,
                    proposal="no_action",
                )
            )
            wiki.append_skill_impact(
                ws,
                prompts.gate_outcome_entry(
                    ws,
                    k,
                    proposal,
                    None,
                    False,
                    "",
                    state["r_best"],
                    status="no_action",
                ),
            )
            wiki.append_log(
                ws, f"iter-{k:02d}: train={train_mean} proposal=no_action"
            )
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            wiki.commit(ws, f"iter-{k:02d}: no action")
            continue

        driver = None
        context = None
        prepared = False
        desc = f"{action} {target}"
        diff = ""
        pre_gates: list[dict] = []
        try:
            driver = assets.resolve_driver(target)
            driver.validate(ws, proposal)
            context = _driver_prepare(driver, target, ws, k, proposal)
            prepared = True
            desc = _driver_apply(driver, target, ws, proposal, context)
            diff = _driver_diff(driver, target, ws, context)
            pre_gates = _driver_pre_gates(driver, target, ws, proposal, context)
        except core_adapter.CoreOperationalError as exc:
            cleanup = None
            rollback_error = None
            if prepared and driver is not None:
                try:
                    cleanup = _driver_rollback(driver, target, ws, context)
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            error = str(exc)
            if rollback_error:
                error += f"; rollback failed: {rollback_error}"
            engineering = _engineering_evidence(
                proposal, context, pre_gates, cleanup=cleanup
            )
            _record_outcome(
                ws,
                state,
                k,
                train_mean,
                proposal,
                f"operational {target}",
                diff,
                status="operational_error",
                accepted=False,
                r_val=None,
                prev_best=state["r_best"],
                error=error,
                engineering=engineering,
            )
            continue
        except ValueError as exc:
            cleanup = None
            if prepared and driver is not None:
                try:
                    cleanup = _driver_rollback(driver, target, ws, context)
                except Exception as rollback_exc:
                    engineering = _engineering_evidence(
                        proposal,
                        context,
                        pre_gates,
                        cleanup={"removed": False, "error": str(rollback_exc)},
                    )
                    _record_outcome(
                        ws,
                        state,
                        k,
                        train_mean,
                        proposal,
                        f"operational {target}",
                        diff,
                        status="operational_error",
                        accepted=False,
                        r_val=None,
                        prev_best=state["r_best"],
                        error=str(rollback_exc),
                        engineering=engineering,
                    )
                    continue
            engineering = _engineering_evidence(
                proposal, context, pre_gates, cleanup=cleanup
            )
            _record_outcome(
                ws,
                state,
                k,
                train_mean,
                proposal,
                f"invalid {target}",
                diff,
                status="invalid",
                accepted=False,
                r_val=None,
                prev_best=state["r_best"],
                error=str(exc),
                engineering=engineering,
            )
            continue

        failed_gate = next(
            (item for item in pre_gates if item.get("status") == "fail"), None
        )
        if failed_gate is not None:
            try:
                cleanup = _driver_rollback(driver, target, ws, context)
            except core_adapter.CoreOperationalError as exc:
                engineering = _engineering_evidence(
                    proposal,
                    context,
                    pre_gates,
                    cleanup={"removed": False, "error": str(exc)},
                )
                _record_outcome(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    status="operational_error",
                    accepted=False,
                    r_val=None,
                    prev_best=state["r_best"],
                    error=str(exc),
                    engineering=engineering,
                )
                continue
            engineering = _engineering_evidence(
                proposal, context, pre_gates, cleanup=cleanup
            )
            _record_outcome(
                ws,
                state,
                k,
                train_mean,
                proposal,
                desc,
                diff,
                status="rejected",
                accepted=False,
                r_val=None,
                prev_best=state["r_best"],
                error=(
                    failed_gate.get("summary")
                    or f"{failed_gate.get('gate')} gate failed"
                ),
                engineering=engineering,
            )
            continue

        if target == "core":
            active = _active_runtime_checked(ws, state)
            if active is None:
                _core_runtime_failure(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    driver,
                    context,
                    pre_gates,
                    runtime_bindings.RuntimeBindingError(
                        "core candidate runtime binding is not configured; "
                        "held-out validation cannot execute the candidate runtime"
                    ),
                )
                continue
            if proposal.get("source_id") != active["source_id"]:
                try:
                    cleanup = _driver_rollback(driver, target, ws, context)
                except Exception as exc:
                    cleanup = {"removed": False, "error": str(exc)}
                engineering = _engineering_evidence(
                    proposal, context, pre_gates, cleanup=cleanup
                )
                _record_outcome(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    f"invalid {target}",
                    diff,
                    status="invalid",
                    accepted=False,
                    r_val=None,
                    prev_best=state["r_best"],
                    error="core proposal source_id is not the active inference source",
                    engineering=engineering,
                )
                continue

            bound = None
            seal = None
            close_result = None
            try:
                seal = _driver_seal(driver, ws, k, context)
                bound = runtime_bindings.bind_sha(
                    ws,
                    proposal["source_id"],
                    seal["candidate_sha"],
                    candidate_worktree=context.worktree,
                )
                if (
                    bound.source_id != active["source_id"]
                    or bound.source_sha != seal["candidate_sha"]
                    or bound.profile_id != active["binding_profile"]
                ):
                    raise runtime_bindings.RuntimeBindingError(
                        "candidate bound runtime identity does not match active source/profile"
                    )
                try:
                    gatek = gating.run_gate(
                        ws,
                        val,
                        k,
                        model=model,
                        runner=bound.runner,
                        dry_run=dry_run,
                        overwrite=True,
                        max_turns=max_turns,
                        runtime_evidence=bound.evidence,
                    )
                finally:
                    close_result = bound.close()
            except (
                runtime_bindings.RuntimeBindingError,
                core_adapter.CoreOperationalError,
                ValueError,
            ) as exc:
                _core_runtime_failure(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    driver,
                    context,
                    pre_gates,
                    exc,
                )
                continue

            r_val = gatek["mean"]
            prev_best = state["r_best"]
            runtime_evidence = {
                "source_id": bound.source_id,
                "source_sha": bound.source_sha,
                "binding_profile": bound.profile_id,
                "fingerprint": bound.fingerprint,
            }
            if r_val <= prev_best:
                try:
                    cleanup = _driver_rollback(driver, target, ws, context)
                except core_adapter.CoreOperationalError as exc:
                    engineering = _engineering_evidence(
                        proposal,
                        context,
                        pre_gates,
                        cleanup={"removed": False, "error": str(exc)},
                        finalize={
                            "seal": seal,
                            "runtime": runtime_evidence,
                            "runtime_close": close_result,
                        },
                    )
                    _record_outcome(
                        ws,
                        state,
                        k,
                        train_mean,
                        proposal,
                        desc,
                        diff,
                        status="operational_error",
                        accepted=False,
                        r_val=r_val,
                        prev_best=prev_best,
                        error=str(exc),
                        engineering=engineering,
                    )
                    continue
                engineering = _engineering_evidence(
                    proposal,
                    context,
                    pre_gates,
                    cleanup=cleanup,
                    finalize={
                        "seal": seal,
                        "runtime": runtime_evidence,
                        "runtime_close": close_result,
                    },
                )
                _record_outcome(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    status="rejected",
                    accepted=False,
                    r_val=r_val,
                    prev_best=prev_best,
                    engineering=engineering,
                )
                continue

            transition = None
            base_finalize = {
                "seal": seal,
                "runtime": runtime_evidence,
                "runtime_close": close_result,
            }
            try:
                transition = _driver_advance_source(driver, ws, k, context)
                active_after = runtime_bindings.active_runtime_config(ws)
                if active_after is None:
                    raise runtime_bindings.RuntimeBindingError(
                        "accepted source transition lost active runtime configuration"
                    )
                new_identity = _runtime_state_identity(active_after)
                if (
                    active_after["source_id"] != active["source_id"]
                    or active_after["binding_profile"] != active["binding_profile"]
                    or active_after["accepted_sha"] != seal["candidate_sha"]
                ):
                    raise runtime_bindings.RuntimeBindingError(
                        "accepted runtime identity does not match transitioned candidate SHA"
                    )

                finalize = {
                    **base_finalize,
                    "transition": transition,
                    "candidate_sha": seal["candidate_sha"],
                    "accepted_sha": transition["accepted_sha"],
                }
                engineering = _engineering_evidence(
                    proposal, context, pre_gates, finalize=finalize
                )
                new_state = copy.deepcopy(state)
                new_state["r_best"] = r_val
                new_state["runtime_identity"] = new_identity
                _append_state_outcome(
                    new_state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    status="accepted",
                    accepted=True,
                    r_val=r_val,
                    engineering=engineering,
                )
                gating.save_state(ws, new_state)
            except Exception as state_exc:
                if transition is None:
                    error = f"core source transition failed: {state_exc}"
                    engineering = _engineering_evidence(
                        proposal,
                        context,
                        pre_gates,
                        finalize={**base_finalize, "error": str(state_exc)},
                    )
                    _record_outcome(
                        ws,
                        state,
                        k,
                        train_mean,
                        proposal,
                        desc,
                        diff,
                        status="operational_error",
                        accepted=False,
                        r_val=r_val,
                        prev_best=prev_best,
                        error=error,
                        engineering=engineering,
                    )
                    continue

                try:
                    compensation = _driver_compensate_source(
                        driver, ws, k, transition, context
                    )
                except Exception as compensation_exc:
                    recovery_error = (
                        "scoring-state persistence/source-finalization failed and "
                        f"source compensation failed: {state_exc}; {compensation_exc}"
                    )
                    recovery_engineering = _engineering_evidence(
                        proposal,
                        context,
                        pre_gates,
                        finalize={
                            **base_finalize,
                            "transition": transition,
                            "state_error": str(state_exc),
                            "compensation_error": str(compensation_exc),
                            "recovery_required": True,
                        },
                    )
                    recovery_state = copy.deepcopy(state)
                    _append_state_outcome(
                        recovery_state,
                        k,
                        train_mean,
                        proposal,
                        desc,
                        status="recovery_required",
                        accepted=False,
                        r_val=r_val,
                        error=recovery_error,
                        engineering=recovery_engineering,
                    )
                    gating.save_state(ws, recovery_state)
                    state.clear()
                    state.update(recovery_state)
                    _audit_outcome_only(
                        ws,
                        state,
                        k,
                        train_mean,
                        proposal,
                        desc,
                        diff,
                        status="recovery_required",
                        accepted=False,
                        r_val=r_val,
                        prev_best=prev_best,
                        error=recovery_error,
                        engineering=recovery_engineering,
                    )
                    continue

                compensated_error = (
                    "scoring-state persistence failed after source transition; "
                    f"source transition compensated: {state_exc}"
                )
                compensated_engineering = _engineering_evidence(
                    proposal,
                    context,
                    pre_gates,
                    finalize={
                        **base_finalize,
                        "transition": transition,
                        "state_error": str(state_exc),
                        "compensation": compensation,
                    },
                )
                compensated_state = copy.deepcopy(state)
                _append_state_outcome(
                    compensated_state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    status="operational_error",
                    accepted=False,
                    r_val=r_val,
                    error=compensated_error,
                    engineering=compensated_engineering,
                )
                gating.save_state(ws, compensated_state)
                state.clear()
                state.update(compensated_state)
                _audit_outcome_only(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    status="operational_error",
                    accepted=False,
                    r_val=r_val,
                    prev_best=prev_best,
                    error=compensated_error,
                    engineering=compensated_engineering,
                )
                continue

            state.clear()
            state.update(new_state)
            release = None
            try:
                release = _driver_release_candidate_ref(driver, ws, k, context)
            except core_adapter.CoreOperationalError as release_exc:
                release = {"removed": False, "error": str(release_exc)}
                wiki.append_log(
                    ws,
                    f"iter-{k:02d}: accepted source/state agree but candidate ref "
                    f"release failed: {release_exc}",
                )
            audit_engineering = _engineering_evidence(
                proposal,
                context,
                pre_gates,
                finalize={
                    **base_finalize,
                    "transition": transition,
                    "candidate_ref_release": release,
                    "candidate_sha": seal["candidate_sha"],
                    "accepted_sha": transition["accepted_sha"],
                },
            )
            _audit_outcome_only(
                ws,
                state,
                k,
                train_mean,
                proposal,
                desc,
                diff,
                status="accepted",
                accepted=True,
                r_val=r_val,
                prev_best=prev_best,
                engineering=audit_engineering,
            )
            continue

        try:
            gatek, _ = _run_gate_with_accepted_runtime(
                ws,
                state,
                val,
                k,
                framework_runner=runner,
                model=model,
                dry_run=dry_run,
                overwrite=True,
                max_turns=max_turns,
            )
        except (runtime_bindings.RuntimeBindingError, ValueError) as exc:
            try:
                cleanup = _driver_rollback(driver, target, ws, context)
            except Exception as rollback_exc:
                cleanup = {"error": str(rollback_exc)}
            engineering = _engineering_evidence(
                proposal, context, pre_gates, cleanup=cleanup
            )
            _record_outcome(
                ws,
                state,
                k,
                train_mean,
                proposal,
                desc,
                diff,
                status="operational_error",
                accepted=False,
                r_val=None,
                prev_best=state["r_best"],
                error=str(exc),
                engineering=engineering,
            )
            continue

        r_val = gatek["mean"]
        prev_best = state["r_best"]
        accepted = r_val > prev_best
        if accepted:
            try:
                finalize = _driver_accept(driver, target, ws, k, r_val, context)
            except core_adapter.CoreOperationalError as exc:
                engineering = _engineering_evidence(
                    proposal,
                    context,
                    pre_gates,
                    finalize={"error": str(exc)},
                )
                _record_outcome(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    status="operational_error",
                    accepted=False,
                    r_val=r_val,
                    prev_best=prev_best,
                    error=str(exc),
                    engineering=engineering,
                )
                continue
            state["r_best"] = r_val
            status = "accepted"
            engineering = _engineering_evidence(
                proposal, context, pre_gates, finalize=finalize
            )
        else:
            try:
                cleanup = _driver_rollback(driver, target, ws, context)
            except core_adapter.CoreOperationalError as exc:
                engineering = _engineering_evidence(
                    proposal,
                    context,
                    pre_gates,
                    cleanup={"removed": False, "error": str(exc)},
                )
                _record_outcome(
                    ws,
                    state,
                    k,
                    train_mean,
                    proposal,
                    desc,
                    diff,
                    status="operational_error",
                    accepted=False,
                    r_val=r_val,
                    prev_best=prev_best,
                    error=str(exc),
                    engineering=engineering,
                )
                continue
            status = "rejected"
            engineering = _engineering_evidence(
                proposal, context, pre_gates, cleanup=cleanup
            )

        _record_outcome(
            ws,
            state,
            k,
            train_mean,
            proposal,
            desc,
            diff,
            status=status,
            accepted=accepted,
            r_val=r_val,
            prev_best=prev_best,
            engineering=engineering,
        )

    return state
'''

path.write_text(prefix + helpers + evolve)

# Improve recovery audit wording in prompts without changing existing labels.
prompts = Path('wikiskill/prompts.py')
ptext = prompts.read_text()
old = '''    elif effective == "operational_error":\n        body += ["", "Validation: operational failure; accepted source state and R_best were not advanced."]\n    elif effective == "rejected" and r_val is None:\n'''
new = '''    elif effective == "operational_error":\n        body += ["", "Validation: operational failure; accepted source state and R_best were not advanced."]\n    elif effective == "recovery_required":\n        body += ["", "Validation: recovery is required because source and scoring state could not be proven consistent; no acceptance is claimed."]\n    elif effective == "rejected" and r_val is None:\n'''
if old not in ptext:
    raise SystemExit('prompts recovery anchor not found')
prompts.write_text(ptext.replace(old, new, 1))
