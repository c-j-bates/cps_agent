# Example transcripts

Verbatim model transcripts for three cherry-picked problems where **GENERATE-Θ** succeeds while most or all baselines fail.

## Format

Each strategy has two files with the same stem:

- `*.json` — structured transcript: `metadata` (problem id, problem text,
  config, model), `execution` (ordered LLM calls: prompt + response per graph
  node), `final_answer`, `terminated_early`.
- `*.md` — the same run rendered as readable Markdown. Note the textual redundancies: for each round, we reproduce the entire prompt that was shown, which includes the full turn histories. So scrolling to the last turn might be expedient.

## minute-cryptic/p16-scoop  (par-1, problem 16)

**Clue:** *"Exclusive school's tall characters expelled by petite principal (5)"* → **SCOOP**
**Model:** DeepSeek chat (baseline_thinking = DeepSeek reasoner) · epochs=3, representative epoch shown.

Wordplay: definition = *Exclusive* (a news scoop); from `SCHOOL` delete the
"tall characters" (H, L) → `SCOO`, then append *petite principal* (first letter
of *petite*, P) → `SCOOP` (deletion + charade).

| Strategy | Final answer | Correct |
|---|---|---|
| **generate_theta** | `SCOOP` | ✓ |
| self_discover | `ELITE` | ✗ |
| step_back | `ELITE` | ✗ |
| iterated_cot | `ELITE` | ✗ |
| baseline | `ELITE` | ✗ |
| baseline_thinking | `ELITE` | ✗ |

GENERATE-Θ is the only strategy to solve it; every other strategy anchors on
*Exclusive school* as the definition (the salient surface reading) → `ELITE`.

## minute-cryptic/id3-one  (par-1, problem 3)

**Clue:** *"20/20 vision eye patch (3)"* → **ONE**
**Model:** Claude Opus 4.6 (instant; baseline_thinking = max-thinking). epochs=1.

| Strategy | Final answer | Correct |
|---|---|---|
| **generate_theta** | `ONE` | ✓ |
| self_discover | `ONE` | ✓ |
| step_back | `OPT` | ✗ |
| iterated_cot | `EVE` | ✗ |
| baseline | `SEE` | ✗ |
| baseline_thinking | `SEE` | ✗ |

Both GENERATE-Θ and self_discover recover `ONE`; the two baselines anchor on the
surface meaning (*vision* → `SEE`) — the max-thinking baseline spends ~37.8k
output tokens and still returns `SEE`.

## bigbench-rosetta/p24  (Rosetta hard, problem 24)

**Task:** few-shot translation into a constructed language.
**Gold:** `pigozosu pinahaze nijize pigozoya piyomoqo guluqo boxihu`
**Model:** DeepSeek v4-pro. epochs=1.

| Strategy | Correct |
|---|---|
| **generate_theta** | ✓ (only strategy to solve it) |
| generate_theta_strict_chain | ✗ |
| self_discover | ✗ |
| step_back | ✗ |
| iterated_cot / iterated_cot_extended | ✗ |
| baseline / baseline_thinking | ✗ |

GENERATE-Θ is the only strategy to get it exactly. Several others (incl. the
thinking baseline) produce near-misses that share a long prefix but diverge by a
morpheme — e.g. the thinking baseline emits `…guluqo viboxihu` (spurious `vi-`)
where gold is `…guluqo boxihu`.
