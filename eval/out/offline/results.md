# Eval results

> Dev split only: rules.yaml and the prompt were written alongside these crashes, so these numbers are optimistic. Add held-out crashes with "split": "test" for numbers you can claim.

Split: **all**. Crashes graded: 30. Sources: eval/out/offline

| Metric | rules_only |
| --- | --- |
| Root-cause category accuracy | 100% (30/30) |
| Culprit frame accuracy | 100% (25/25) |
| Null culprit when no app frame applies | 80% (4/5) |
| Severity exact | 60% (18/30) |
| Severity within one level | 100% (30/30) |
| Fix usefulness (0-2, hand graded) | not graded yet |
| Schema valid on first try | n/a (no model) |
| Invalid after repair retry | 0 |
| Grounding flags (culprit not in input) | 0 |
| Latency p50 (s) | n/a |
| Latency p95 (s) | n/a |
| Avg tokens in / out | n/a |
| Cost per crash (USD) | 0 (no model) |

## Per category (correct / total)

| Category | rules_only |
| --- | --- |
| anr_blocking | 3/3 |
| compose_runtime | 2/2 |
| concurrency | 1/1 |
| coroutine | 2/2 |
| database | 1/1 |
| lifecycle_state | 4/4 |
| main_thread_violation | 2/2 |
| memory | 2/2 |
| native | 1/1 |
| null_safety | 4/4 |
| parcel_binder | 2/2 |
| permission_security | 2/2 |
| r8_build_config | 1/1 |
| resources | 1/1 |
| window_ui | 2/2 |

## Category misses

**rules_only** (0): none
