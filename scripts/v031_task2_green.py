from pathlib import Path

core = Path('wikiskill/core_adapter.py')
text = core.read_text()
start = text.index('def reject_candidate(session: CandidateSession) -> dict:\n')
end = text.index('def audit_evidence(session: CandidateSession) -> dict:\n')
new = r'''def _candidate_ref(session: CandidateSession, iteration: int) -> str:
    return f"refs/wikiskill/{session.source_id}/candidate-{iteration}"


def _read_exact_ref(session: CandidateSession, ref: str) -> str | None:
    p = _git(session.repo, "rev-parse", "--verify", ref)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def _delete_candidate_ref(session: CandidateSession, iteration: int) -> dict:
    if session.candidate_sha is None:
        return {"removed": True, "candidate_ref": None, "already_missing": True}
    ref = _candidate_ref(session, iteration)
    current = _read_exact_ref(session, ref)
    if current is None:
        return {"removed": True, "candidate_ref": ref, "already_missing": True}
    if current != session.candidate_sha:
        raise CoreOperationalError(
            f"candidate ref {ref} changed unexpectedly: {current}"
        )
    p = _git(session.repo, "update-ref", "-d", ref, session.candidate_sha)
    if p.returncode != 0:
        raise CoreOperationalError(
            f"could not remove candidate ref {ref}: {p.stderr.strip()}"
        )
    if _read_exact_ref(session, ref) is not None:
        raise CoreOperationalError(f"candidate ref remained after deletion: {ref}")
    return {"removed": True, "candidate_ref": ref}


def reject_candidate(session: CandidateSession) -> dict:
    cleanup = _cleanup_worktree(session)
    ref_cleanup = _delete_candidate_ref(session, session.iteration)
    return {**cleanup, "candidate_ref_cleanup": ref_cleanup}


def _stage_expected_files(session: CandidateSession) -> None:
    p = _git(session.worktree, "add", "--", *session.changed_files)
    if p.returncode != 0:
        raise CoreOperationalError(f"could not stage core candidate files: {p.stderr.strip()}")
    staged = _git(session.worktree, "diff", "--cached", "--name-only", "HEAD", "--")
    if staged.returncode != 0:
        raise CoreOperationalError(f"could not inspect staged core candidate: {staged.stderr.strip()}")
    actual = tuple(sorted(line.strip() for line in staged.stdout.splitlines() if line.strip()))
    if actual != session.changed_files:
        raise ValueError(
            f"staged core candidate scope changed: expected={session.changed_files} actual={actual}"
        )


def seal_candidate(session: CandidateSession, iteration: int) -> dict:
    if not session.applied:
        raise CoreOperationalError("cannot seal an unapplied core candidate")
    if session.candidate_sha is not None:
        raise CoreOperationalError("core candidate is already sealed")
    _verify_integrity(session)
    _stage_expected_files(session)
    p = _git(
        session.worktree,
        "-c",
        "user.email=wikiskill@local",
        "-c",
        "user.name=wikiskill-evolution",
        "commit",
        "-q",
        "-m",
        f"wikiskill core candidate iter-{iteration}",
    )
    if p.returncode != 0:
        raise CoreOperationalError(f"could not commit core candidate: {p.stderr.strip()}")
    head = _git(session.worktree, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise CoreOperationalError(f"could not resolve core candidate commit: {head.stderr.strip()}")
    candidate_sha = head.stdout.strip()
    ref = _candidate_ref(session, iteration)
    temp = _git(session.repo, "update-ref", ref, candidate_sha, "")
    if temp.returncode != 0:
        raise CoreOperationalError(
            f"could not create temporary candidate ref: {temp.stderr.strip()}"
        )
    session.candidate_sha = candidate_sha
    return {
        "source_id": session.source_id,
        "base_sha": session.base_sha,
        "candidate_sha": candidate_sha,
        "candidate_ref": ref,
    }


def advance_accepted_candidate(session: CandidateSession, iteration: int) -> dict:
    if session.candidate_sha is None:
        raise CoreOperationalError("core candidate must be sealed before source transition")
    ref = _candidate_ref(session, iteration)
    if _read_exact_ref(session, ref) != session.candidate_sha:
        raise CoreOperationalError("sealed candidate ref does not match candidate SHA")
    cleanup = _cleanup_worktree(session)
    try:
        state = sources.advance_accepted_sha(
            session.ws,
            session.source_id,
            session.base_sha,
            session.candidate_sha,
            iteration,
        )
    except sources.SourceOperationalError as exc:
        raise CoreOperationalError(str(exc)) from exc
    return {
        "source_id": session.source_id,
        "base_sha": session.base_sha,
        "candidate_sha": session.candidate_sha,
        "accepted_sha": state["accepted_sha"],
        "candidate_ref": ref,
        "cleanup": cleanup,
    }


def compensate_accepted_candidate(
    session: CandidateSession, transition: dict, iteration: int
) -> dict:
    if session.candidate_sha is None:
        raise CoreOperationalError("cannot compensate an unsealed core candidate")
    if transition.get("accepted_sha") != session.candidate_sha:
        raise CoreOperationalError("source transition evidence does not match candidate SHA")
    if transition.get("base_sha") != session.base_sha:
        raise CoreOperationalError("source transition evidence does not match base SHA")
    current = sources.accepted_sha(session.ws, session.source_id)
    if current != session.candidate_sha:
        raise CoreOperationalError(
            f"cannot compensate source transition from unexpected accepted SHA {current}"
        )
    try:
        state = sources.advance_accepted_sha(
            session.ws,
            session.source_id,
            session.candidate_sha,
            session.base_sha,
            iteration,
        )
    except sources.SourceOperationalError as exc:
        raise CoreOperationalError(str(exc)) from exc
    return {
        "source_id": session.source_id,
        "candidate_sha": session.candidate_sha,
        "restored_sha": state["accepted_sha"],
        "candidate_ref": _candidate_ref(session, iteration),
    }


def release_candidate_ref(session: CandidateSession, iteration: int) -> dict:
    return _delete_candidate_ref(session, iteration)


def accept_candidate(session: CandidateSession, iteration: int) -> dict:
    """V0.3 compatibility wrapper; V0.3.1 harness uses split transaction APIs."""
    if session.candidate_sha is None:
        seal_candidate(session, iteration)
    transition = advance_accepted_candidate(session, iteration)
    released = release_candidate_ref(session, iteration)
    return {**transition, "temporary_ref_removed": released["removed"]}


'''
core.write_text(text[:start] + new + text[end:])

assets = Path('wikiskill/assets.py')
text = assets.read_text()
old = '''    def accept(self, ws: str, iteration: int, score: float, context=None):
        if context is None:
            raise ValueError("core accept requires candidate context")
        return core_adapter.accept_candidate(context, iteration)

    def rollback(self, ws: str, context=None):
        if context is None:
            raise ValueError("core rollback requires candidate context")
        return core_adapter.reject_candidate(context)
'''
new = '''    def seal(self, ws: str, iteration: int, context=None):
        if context is None:
            raise ValueError("core seal requires candidate context")
        return core_adapter.seal_candidate(context, iteration)

    def advance_source(self, ws: str, iteration: int, context=None):
        if context is None:
            raise ValueError("core source transition requires candidate context")
        return core_adapter.advance_accepted_candidate(context, iteration)

    def compensate_source(self, ws: str, iteration: int, transition: dict, context=None):
        if context is None:
            raise ValueError("core source compensation requires candidate context")
        return core_adapter.compensate_accepted_candidate(context, transition, iteration)

    def release_candidate_ref(self, ws: str, iteration: int, context=None):
        if context is None:
            raise ValueError("core candidate ref release requires candidate context")
        return core_adapter.release_candidate_ref(context, iteration)

    def accept(self, ws: str, iteration: int, score: float, context=None):
        if context is None:
            raise ValueError("core accept requires candidate context")
        return core_adapter.accept_candidate(context, iteration)

    def rollback(self, ws: str, context=None):
        if context is None:
            raise ValueError("core rollback requires candidate context")
        return core_adapter.reject_candidate(context)
'''
if old not in text:
    raise SystemExit('CoreDriver accept/rollback anchor not found')
assets.write_text(text.replace(old, new, 1))
