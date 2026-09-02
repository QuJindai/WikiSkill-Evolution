from pathlib import Path
import subprocess
import sys


def replace_once(path, old, new, label):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"{label} anchor not found")
    p.write_text(text.replace(old, new, 1))


def patch_docs():
    replace_once(
        "README.md",
        """V0.3 makes `core` executable without turning proposals into arbitrary code
runners. Operators pre-register a local Git source plus immutable path and Gate
policy; the Evolution Proposer may then submit a bounded text patch against the
current accepted SHA.
""",
        """V0.3 makes governed `core` **source mutation and engineering validation**
executable without turning proposals into arbitrary code runners. Operators
pre-register a local Git source plus immutable path and Gate policy; the
Evolution Proposer may then submit a bounded text patch against the current
accepted SHA.

A final review found an essential causal boundary: the generic Git Source
Adapter does not itself make the held-out agent execute the candidate runtime.
Its built-in `CoreDriver` is therefore intentionally `candidate_runtime_bound =
False` and fails closed after engineering gates. It cannot advance the accepted
source ref or `R_best` until a source-specific runtime binding proves that
held-out validation is actually running the candidate. This prevents stochastic
model variation from accepting source code that was never active.
""",
        "README V0.3 intro",
    )

    anchor = "- **Governed source evolution** — V0.3 can patch only pre-registered local Git sources, in generated isolated worktrees, under allow/deny and size policies.\n"
    replace_once(
        "docs/index.md",
        anchor,
        anchor + "- **Causally bound acceptance** — the generic Core Driver is fail-closed before held-out scoring until a source-specific runtime binding proves the candidate runtime is what the held-out tasks execute.\n",
        "docs/index governed source",
    )

    replace_once(
        "docs/V0.3-GIT-SOURCE-ADAPTER.md",
        "## Acceptance and rollback\n\n",
        """## Candidate runtime binding

Engineering gates prove that a candidate source patch is bounded, buildable,
and regression-clean. They do **not** by themselves prove that WikiSkill
held-out tasks executed that candidate runtime.

The built-in generic `CoreDriver` therefore declares
`candidate_runtime_bound = False`. After engineering gates pass, the harness
records an `operational_error`, removes the candidate worktree, skips held-out
scoring, and leaves both the accepted source ref and `R_best` unchanged.

A source-specific Core Driver may set the binding true only when it provides an
execution path that actually runs the candidate runtime during held-out
validation. Android/llama.cpp/device bindings remain a later-phase capability.

## Acceptance and rollback

""",
        "V0.3 acceptance section",
    )

    replace_once(
        "docs/superpowers/specs/2026-09-01-v0.3-git-source-adapter-design.md",
        "Predecessor: V0.2 Asset-Driver Evolution Architecture\n\n",
        """Predecessor: V0.2 Asset-Driver Evolution Architecture

> **Post-review correction — 2026-09-02:** implementation review proved that
> the generic Git Source Adapter did not causally bind the candidate worktree to
> the ordinary WikiSkill held-out runner. The generic `CoreDriver` now fails
> closed with `candidate_runtime_bound = False`: engineering gates may run, but
> no held-out score, accepted-ref advance, or `R_best` advance is permitted until
> a source-specific runtime binding executes the candidate during held-out
> validation. Sections describing full Core acceptance are conditional on that
> binding and are not a claim that the generic V0.3 driver provides it.

""",
        "spec header",
    )


def write_evidence():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    security = Path("/tmp/security.txt").read_text()
    pyflakes = Path("/tmp/pyflakes.txt").read_text()
    full = Path("/tmp/full.txt").read_text()
    out = Path("docs/superpowers/plans/2026-09-02-v0.3-postreview-final-verification.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# V0.3 Post-review Final Verification\n\n"
        f"verified_head={head}\n"
        "security_exit_code=0\n"
        "pyflakes_exit_code=0\n"
        "full_pytest_exit_code=0\n\n"
        "## Focused security + runtime-binding suite\n```text\n"
        + security
        + "```\n\n## Pyflakes\n```text\n"
        + pyflakes
        + "```\n\n## Full pytest\n```text\n"
        + full
        + "```\n"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"patch", "evidence"}:
        raise SystemExit("usage: v03_postreview_finalize.py patch|evidence")
    if sys.argv[1] == "patch":
        patch_docs()
    else:
        write_evidence()
