# Paper run results

Updated: 2026-10-26T01:25:34.070382+00:00

The egglog-cracked measurements use the local merged base with `--no-decomp`. Baseline engines are pinned independently.

| Experiment | State | Recorded measurements | Outcomes |
|---|---|---:|---|
| scaling | complete | 90 | ok: 90 |
| scaling-real-world | complete | 486 | ok: 486 |
| e2e | complete | 486 | ok: 486 |
| storel | complete | 36 | ok: 36 |
| scaling-storel | complete | 36 | ok: 36 |
| storel-schedule | complete | 144 | ok: 144 |
| ablation | complete | 752 | ok: 732, timeout: 20 |
| ablation-storel | complete | 54 | ok: 54 |
| engines | complete | 521 | ok: 520, timeout: 1 |
| query | complete | 3437 |  |

`complete` means every planned configuration was attempted. Timeouts, OOMs, and failures remain explicit outcomes; they are not successful runtime measurements.

## Figures

| Comparison | PDF | Preview |
|---|---|---|
| Algebraic engines | [PDF](../../figures/paper/engines.pdf) | [PNG](../../figures/paper/engines.png) |
| Real-world engines | [PDF](../../figures/paper/real-world.pdf) | [PNG](../../figures/paper/real-world.png) |
| STOREL backoff time and work | [PDF](../../figures/paper/storel-backoff.pdf) | [PNG](../../figures/paper/storel-backoff.png) |
| Algebraic scaling | [PDF](../../figures/paper/scaling-algebraic.pdf) | [PNG](../../figures/paper/scaling-algebraic.png) |
| Real-world 8-thread scaling | [PDF](../../figures/paper/scaling-real-world.pdf) | [PNG](../../figures/paper/scaling-real-world.png) |
| STOREL schedule scaling | [PDF](../../figures/paper/storel-schedule.pdf) | [PNG](../../figures/paper/storel-schedule.png) |
| STOREL schedule work | [PDF](../../figures/paper/storel-schedule-work.pdf) | [PNG](../../figures/paper/storel-schedule-work.png) |
| Ablation: static size order | [PDF](../../figures/paper/ablation.pdf) | [PNG](../../figures/paper/ablation.png) |
| Ablation: static refinement order | [PDF](../../figures/paper/ablation-refinement.pdf) | [PNG](../../figures/paper/ablation-refinement.png) |
| Database query totals | [PDF](../../figures/paper/query-totals.pdf) | [PNG](../../figures/paper/query-totals.png) |
| Database query scatter | [PDF](../../figures/paper/query-scatter.pdf) | [PNG](../../figures/paper/query-scatter.png) |

## Completed real-world scaling

| Suite | 8-thread geometric mean speedup | Completed pairs |
|---|---:|---:|
| eggcc | 0.871× | 32 |
| hardboiled | 1.109× | 42 |
| luminal | 0.897× | 7 |
| storel | 0.934× | 6 |

## Real-world runtime comparison

| Suite | Geometric mean speedup of cracked | Completed pairs | Pairs with different table sizes |
|---|---:|---:|---:|
| hardboiled | 4.912× | 42/42 | 0 |
| luminal | 75.232× | 7/7 | 0 |
| eggcc | 3.327× | 32/32 | 32 |
| storel | 3.806× | 6/6 | 6 |

[Figure](../../figures/paper/real-world.pdf); [per-benchmark details](../../figures/paper/real-world-details.pdf); [CSV](../../figures/paper/real-world.csv).

Hardboiled, Luminal and eggcc use egglog-prior as baseline; STOREL uses egg with backoff.

| Baseline | Equal weight per application suite | Equal weight per benchmark | Completed benchmarks |
|---|---:|---:|---:|
| egglog-prior | 10.712× | 5.331× | 81/81 |
| egg | 3.806× | 3.806× | 6/6 |

The first aggregate averages the suite geometric means; the second weights all individual benchmark pairs equally. Both use ratios of three-run median wall times at one thread.

## Ablation: full static size order

| Suite | Geometric mean speedup of cracked | Completed pairs | Pairs with different table sizes |
|---|---:|---:|---:|
| math-microbenchmark | 1.013× | 2/2 | 0 |
| horner | 1.003× | 2/2 | 0 |
| matmul-assoc | 0.999× | 3/3 | 0 |
| hardboiled | 196.771× | 30/42 | 0 |
| luminal | 70.797× | 3/7 | 0 |
| eggcc | 4.604× | 28/32 | 4 |
| storel | 3.068× | 6/6 | 0 |

[Figure](../../figures/paper/ablation.pdf); [per-benchmark details](../../figures/paper/ablation-details.pdf); [CSV](../../figures/paper/ablation.csv).

The named no-DVO baseline is 1.0×. Both figures share the same dynamic measurements. Incomplete pairs are excluded from the geometric mean and remain explicit in the CSV/details.

## Ablation: full static refinement order

| Suite | Geometric mean speedup of cracked | Completed pairs | Pairs with different table sizes |
|---|---:|---:|---:|
| math-microbenchmark | 1.013× | 2/2 | 0 |
| horner | 1.025× | 2/2 | 0 |
| matmul-assoc | 1.034× | 3/3 | 0 |
| hardboiled | 46.987× | 42/42 | 0 |
| luminal | 3.522× | 7/7 | 0 |
| eggcc | 0.932× | 32/32 | 5 |
| storel | 1.048× | 6/6 | 0 |

[Figure](../../figures/paper/ablation-refinement.pdf); [per-benchmark details](../../figures/paper/ablation-refinement-details.pdf); [CSV](../../figures/paper/ablation-refinement.csv).

The named no-DVO baseline is 1.0×. Both figures share the same dynamic measurements. Incomplete pairs are excluded from the geometric mean and remain explicit in the CSV/details.

## STOREL schedule study

Each rewrite phase runs K times before aux saturation, at K = 1, 2, 4, 8. Speedups include only runs that reach their node target.

| Rewrite batch | Geometric mean 8-thread speedup | Kernels reaching target at both thread counts |
|---:|---:|---:|
| 1 | 0.934× | 6/6 |
| 2 | 1.065× | 5/6 |
| 4 | 1.047× | 3/6 |
| 8 | 1.151× | 2/6 |

The following configurations are excluded from speedups because at least one repetition stopped below target:

| Kernel | K | Threads | Repetitions reaching target | Reached nodes, min–max | Target |
|---|---:|---:|---:|---:|---:|
| mmm-sum-2nd | 2 | 1 | 0/3 | 5,681–5,681 | 10,000 |
| mmm-sum-2nd | 2 | 8 | 0/3 | 5,681–5,681 | 10,000 |
| mmm-sum-2nd | 4 | 1 | 0/3 | 3,385–3,385 | 10,000 |
| mmm-sum-2nd | 4 | 8 | 0/3 | 3,385–3,385 | 10,000 |
| mmm-sum-2nd | 8 | 1 | 0/3 | 3,413–3,413 | 10,000 |
| mmm-sum-2nd | 8 | 8 | 0/3 | 3,413–3,413 | 10,000 |
| mmm-2nd | 8 | 1 | 0/3 | 144,136–144,136 | 150,000 |
| mmm-2nd | 8 | 8 | 0/3 | 146,326–146,474 | 150,000 |
| mttkrp-2nd | 4 | 1 | 0/3 | 25,728–25,728 | 1,000,000 |
| mttkrp-2nd | 4 | 8 | 0/3 | 25,728–25,728 | 1,000,000 |
| mttkrp-2nd | 8 | 8 | 2/3 | 882,256–1,015,398 | 1,000,000 |
| batax-2nd | 4 | 8 | 1/3 | 915,912–1,035,082 | 1,000,000 |
| batax-2nd | 8 | 1 | 0/3 | 977,084–977,084 | 1,000,000 |
| batax-2nd | 8 | 8 | 1/3 | 904,654–1,015,551 | 1,000,000 |

[Timing/scaling figure](../../figures/paper/storel-schedule.pdf); [reached-work figure](../../figures/paper/storel-schedule-work.pdf); [CSV](../../figures/paper/storel-schedule.csv). Schedule changes can change the explored e-graph even at similar reached sizes.

## Algebraic engine comparison

[All seven workloads](../../figures/paper/engines.pdf); [CSV with all outcomes](../../figures/paper/engines.csv).

| Workload | cracked 1-thread median | cracked 16-thread median |
|---|---:|---:|
| [math-microbenchmark](../../figures/paper/engines-math-microbenchmark.pdf) | 0.2947 s | 0.1213 s |
| [math-microbenchmark-12](../../figures/paper/engines-math-microbenchmark-12.pdf) | 7.469 s | 1.279 s |
| [horner-9](../../figures/paper/engines-horner-9.pdf) | 0.4921 s | 0.1898 s |
| [horner-10](../../figures/paper/engines-horner-10.pdf) | 2.324 s | 0.4735 s |
| [matmul-assoc-320](../../figures/paper/engines-matmul-assoc-320.pdf) | 0.8142 s | 0.1383 s |
| [matmul-assoc-480](../../figures/paper/engines-matmul-assoc-480.pdf) | 2.877 s | 0.32 s |
| [matmul-assoc-640](../../figures/paper/engines-matmul-assoc-640.pdf) | 7.286 s | 0.7177 s |

Against the single-thread oatlog baseline, the ratios below are oatlog median / cracked median. Values above 1 favor cracked. All times are medians of five runs.

| Workload | Cracked 1T | Cracked 4T | Cracked 8T | Cracked 16T |
|---|---:|---:|---:|---:|
| math-microbenchmark | 0.560× | 1.070× | 1.216× | 1.361× |
| math-microbenchmark-12 | 0.602× | 1.924× | 2.830× | 3.514× |
| horner-9 | 0.252× | 0.460× | 0.597× | 0.652× |
| horner-10 | 0.219× | 0.601× | 0.902× | 1.075× |
| matmul-assoc-320 | 0.195× | 0.613× | 0.954× | 1.146× |
| matmul-assoc-480 | 0.192× | 0.708× | 1.243× | 1.724× |
| matmul-assoc-640 | 0.187× | 0.684× | 1.217× | 1.894× |
| Geometric mean | 0.278× | 0.776× | 1.151× | 1.435× |

## Database query comparison

3437 translated queries; 727 nonempty queries completed by all three engines; 0 output row-count mismatches. Totals below cover only that shared nonempty subset.

| Engine | Optimize/compile total | Execute total | Unsuccessful queries |
|---|---:|---:|---:|
| egglog-cracked | 77.23 ms | 97.01 ms | 0 |
| DuckDB | 579.69 ms | 850.16 ms | 0 |
| Umbra | 22503.44 ms | 402.25 ms | 57 |

Optimization speedup is database preparation time divided by cracked preparation time; values above 1 favor cracked. Geometric means below weight each shared successful nonempty query equally.

| Program | Shared nonempty queries | Geometric mean vs DuckDB | Geometric mean vs Umbra |
|---|---:|---:|---:|
| gemma | 101 | 4.228× | 252.080× |
| gemma4_moe | 112 | 4.171× | 239.183× |
| llama | 103 | 4.757× | 259.949× |
| paged_llama | 101 | 4.876× | 269.102× |
| qwen | 104 | 4.792× | 263.109× |
| qwen3_moe | 111 | 4.912× | 290.318× |
| whisper | 95 | 4.485× | 267.611× |
| All queries | 727 | 4.594× | 262.589× |

The ratios of total optimization times over the same queries are **7.506× vs DuckDB** and **291.369× vs Umbra**. These are distinct from the per-query geometric means.

For **execution time**, excluding preparation, the per-query geometric means are **13.249× vs DuckDB** and **6.510× vs Umbra**; the total-time ratios are **8.764× / 4.147×**. Cracked is faster on 719/727 and 703/727 queries, respectively. Egglog execution is search + apply + merge, excluding rebuild.

For the per-model bars, another useful aggregate weights each model equally: compute each model’s total-time ratio, then take the geometric mean across models. This differs from weighting each query equally.

| Metric | Geometric mean of model total-time ratios vs DuckDB | vs Umbra | Models |
|---|---:|---:|---:|
| Optimize/compile | 7.538× | 293.126× | 7 |
| Execute | 11.504× | 5.180× | 7 |

[Totals](../../figures/paper/query-totals.pdf); [per-query scatter](../../figures/paper/query-scatter.pdf); [CSV](../../figures/paper/query.csv). Timing boundaries differ by engine as specified in the run guide.

## Result-size checks

The real-world comparisons measure the original schedules. All 32 eggcc programs have cross-version table-size differences; those same programs differed in the historical comparison. In cracked-only 1/8-thread measurements, 24 eggcc programs vary in table sizes. These measurements do not establish identical work or extracted-program equivalence. STOREL is node-limited and can overshoot its target differently. See the [guide](README.md) and [audit](audit.json) for per-experiment evidence.

## Files

- [Run configuration and interpretation](README.md)
- [Live status](status.json)
- [Settings and size audit](audit.json)
- [Figure checksums](figures.json)
- [Real-world aggregates by baseline and weighting](real-world-speedups.json)
- [Oatlog comparison by thread count](oatlog-speedups.json)
- [Fresh database preparation speedups](query-speedups.json)
- [Fresh database execution speedups](query-runtime-speedups.json)
- [Historical database preparation speedups](historical-query-speedups.json)
- [Historical database execution speedups](historical-query-runtime-speedups.json)
- Figures and CSVs: `../../figures/paper/`
- Individual run logs: `../../runs/<run-id>/logs/`
- Queue/build/check logs: `../../logs/paper/`
