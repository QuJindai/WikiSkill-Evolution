# WikiSkill for Hermes

**Compile agent experience into a persistent wiki — and let governed assets evolve.**

WikiSkill is based on the Google Research paper arXiv:2608.27454. This repository is an independent MIT-licensed evolution workspace based on the community implementation, with a live documented run log, V0.2 multi-asset evolution, and the V0.3 governed Git Source Adapter.

## The loop

```text
raw experience -> persistent wiki -> target-aware proposal -> Asset Driver
(session traces)   (pattern pages)    (skill/prompt/harness/core)
                                               ↓
                               engineering gates for core
                                               ↓
                                  held-out R_val > R_best ?
```

1. **Raw layer** — agents execute tasks; full transcripts are captured and remain immutable.
2. **Wiki maintainer** — distills low-scoring traces into persistent pattern pages.
3. **Evolution proposer** — can propose `skill`, `prompt`, declarative `harness`, or a bounded `core` patch against a pre-registered trusted source.
4. **Engineering gates** — Core candidates pass Static → Build → Regression → optional Performance before held-out validation.
5. **Held-out gating** — every scored candidate is accepted iff `R_val > R_best`; rejection rolls back only the candidate while the Wiki remains.

See [V0.2 Multi-Asset Evolution](V0.2-ASSET-DRIVERS.md) for the Asset Driver foundation and [V0.3 Git Source Adapter](V0.3-GIT-SOURCE-ADAPTER.md) for source manifests, accepted Git refs, Core proposal schema, engineering gates, CLI, and rollback semantics.

## Why this repo

- **One strict capability gate** — evolved assets still require strict held-out improvement.
- **Governed source evolution** — V0.3 can patch only pre-registered local Git sources, in generated isolated worktrees, under allow/deny and size policies.
- **Causally bound acceptance** — the generic Core Driver is fail-closed before held-out scoring until a source-specific runtime binding proves the candidate runtime is what the held-out tasks execute.
- **No proposal shell** — repository identity and Gate profiles come from trusted registries; proposal data cannot become executable configuration.
- **Authoritative accepted Git ref** — `refs/wikiskill/<source_id>/accepted` preserves accepted source provenance and reachability.
- **Backward compatible** — legacy proposals without `target` still behave as skill proposals; V0.2 Skill/Prompt/Harness behavior remains covered by regression tests.
- **Honest by construction** — fresh task sandboxes, zero-tool-call detection, engineering-gate short-circuiting, full rejected/invalid proposal audit, and paired statistical comparison remain intact.

## Quickstart

```bash
pip install -e .
wikiskill init demo
wikiskill evolve demo --iters 1 --model google/gemini-2.5-flash-lite --provider openrouter
wikiskill status demo
```

For governed source operations from a workspace:

```bash
wikiskill source register source.json
wikiskill source list
wikiskill source inspect <source_id>
wikiskill source validate <source_id>
```

There is intentionally no arbitrary `source exec` command.

See the repository README for the full CLI and the [Run Log](RUNS.md) for historical real-model experiments.
