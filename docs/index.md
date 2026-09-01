# WikiSkill for Hermes

**Compile agent experience into a persistent wiki — and let governed assets evolve.**

WikiSkill is based on the Google Research paper arXiv:2608.27454. This repository is an independent MIT-licensed evolution workspace based on the community implementation, with a live documented run log and a V0.2 multi-asset extension.

## The loop

```text
raw experience -> persistent wiki -> target-aware proposal -> Asset Driver -> gated rollout
(session traces)   (pattern pages)    (skill/prompt/harness)   R_val > R_best ?
```

1. **Raw layer** — agents execute tasks; full transcripts are captured and remain immutable.
2. **Wiki maintainer** — distills low-scoring traces into persistent pattern pages.
3. **Evolution proposer** — can propose `skill`, `prompt`, or declarative `harness` candidates; `core` is a fail-closed contract in V0.2.
4. **Gating** — the candidate runs on held-out tasks and is accepted iff `R_val > R_best`; rejection rolls back only the candidate asset while the Wiki remains.

See [V0.2 Multi-Asset Evolution](V0.2-ASSET-DRIVERS.md) for schemas, asset paths, policy precedence, and safety boundaries.

## Why this repo

- **One common gate** — skill, prompt, and harness candidates share the same strict held-out validation rule.
- **Backward compatible** — legacy proposals without `target` still behave as skill proposals.
- **Fail closed** — invalid targets/policies skip validation, and core/source mutation is not executable in V0.2.
- **Honest by construction** — fresh sandboxes, zero-tool-call detection, full rejected/invalid proposal audit, and paired statistical comparison remain intact.

## Quickstart

```bash
pip install -e .
wikiskill init demo
wikiskill evolve demo --iters 1 --model google/gemini-2.5-flash-lite --provider openrouter
wikiskill status demo
```

See the repository README for the full CLI and the [Run Log](RUNS.md) for historical real-model experiments.
