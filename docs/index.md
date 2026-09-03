# WikiSkill for Hermes

**Compile agent experience into a persistent wiki — and let governed assets evolve.**

WikiSkill is based on the Google Research paper arXiv:2608.27454. This repository is an independent MIT-licensed evolution workspace based on the community implementation, with a live documented run log, V0.2 multi-asset evolution, V0.3 governed Git Source Adapter, and V0.3.1 trusted runtime binding.

## The loop

```text
raw experience -> persistent wiki -> target-aware proposal -> Asset Driver
(session traces)   (pattern pages)    (skill/prompt/harness/core)
                                               ↓
                               engineering gates for core
                                               ↓
                            exact accepted/candidate runtime
                                               ↓
                                  held-out R_val > R_best ?
```

1. **Raw layer** — agents execute tasks; full transcripts are captured and remain immutable.
2. **Wiki maintainer** — distills low-scoring traces into persistent pattern pages.
3. **Evolution proposer** — can propose `skill`, `prompt`, declarative `harness`, or a bounded `core` patch against a pre-registered trusted source.
4. **Engineering gates** — Core candidates pass Static → Build → Regression → optional Performance before capability scoring.
5. **Trusted runtime binding** — normal inference is pinned to the accepted source SHA; a Core candidate is sealed and its exact SHA is what the held-out tasks execute.
6. **Held-out gating** — every scored candidate is accepted iff `R_val > R_best`; rejection rolls back only the candidate while the Wiki remains.

See [V0.2 Multi-Asset Evolution](V0.2-ASSET-DRIVERS.md) for the Asset Driver foundation, [V0.3 Git Source Adapter](V0.3-GIT-SOURCE-ADAPTER.md) for governed source mutation, and [V0.3.1 Trusted Runtime Binding](V0.3.1-RUNTIME-BINDING.md) for exact-SHA execution, runtime identity, transaction ordering, and recovery semantics.

## Why this repo

- **One strict capability gate** — evolved assets still require strict held-out improvement.
- **Governed source evolution** — V0.3 can patch only pre-registered local Git sources, in generated isolated worktrees, under allow/deny and size policies.
- **Causally bound acceptance** — V0.3.1 proves which exact accepted/candidate Git SHA produced baseline, train, and held-out execution; a boolean binding flag is never sufficient evidence.
- **No proposal shell** — repository identity, Gate profiles, runtime profiles, entrypoints, argv, environment, and timeouts come from trusted registries/code; proposal data cannot become executable configuration.
- **Authoritative accepted Git ref** — `refs/wikiskill/<source_id>/accepted` preserves accepted source provenance and reachability.
- **Transactional Core finalization** — source A→B precedes atomic score/runtime state persistence; state failure compensates B→A or enters fail-closed recovery.
- **Backward compatible** — legacy proposals without `target` still behave as skill proposals; V0.2 Skill/Prompt/Harness behavior remains covered by regression tests.
- **Honest by construction** — fresh task sandboxes, zero-tool-call detection, engineering-gate short-circuiting, runtime SHA trace evidence, full rejected/invalid proposal audit, and paired statistical comparison remain intact.

## Quickstart

```bash
pip install -e .
wikiskill init demo
wikiskill evolve demo --iters 1 --model google/gemini-2.5-flash-lite --provider openrouter
wikiskill status demo
```

For governed source + runtime operations from a workspace:

```bash
wikiskill source register source.json
wikiskill source validate <source_id>
wikiskill runtime bind <source_id> registered:python-json-runner-v1
wikiskill runtime activate <source_id>
wikiskill runtime inspect
wikiskill evolve <domain> --ws <workspace> --iters 1
```

`runtime activate` must happen before the initial baseline. There is intentionally no arbitrary `source exec`, `runtime exec`, or runtime command-registration surface.

See the repository README for the full CLI and the [Run Log](RUNS.md) for historical real-model experiments.
