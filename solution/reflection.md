# Reflection (≤1 page)

**Results (final = v2).**

| Version | Practice | Public | **Private** |
|---|---|---|---|
| v1: z ≥ 2.5σ, two-sided, warm-up on bare bound | 50.0 (TPR 1.00 / FPR 0.00) | 45.12 (0.95 / 0.017) | 32.41 (0.648 / 0.000) |
| **v2: z ≥ 2.0σ, one-sided in fault direction, priors** | 49.31 (1.00 / 0.023) | 46.45 (1.00 / 0.058) | **32.72 (0.667 / 0.021)** |

No phase had cost overage except public (240/220, every version). Private was run twice in total, once
per version, and was never tuned in a loop. v2 was chosen from the cost argument below and checked on
practice/public only.

**Approach.** I make one metered call per event and run two kinds of checks:
- *Structural:* contract violations, freshness delay above the declared SLA, a declared or usual upstream
  edge missing, and orphan outputs (downstream count = 0).
- *Statistical:* σ comes from `ctx.baseline` (mean ± 3σ). The centre is a prior blended with an online
  median of clean-looking events. Each test is one-sided in the fault direction (spike, lag, drift,
  staleness), with a 3σ check in the opposite direction. I also alert on a sum of z² when several
  metrics are only moderately elevated.

**Which fault types were hardest to catch, and why?**
- The structural faults (schema/type violations, missing upstream, orphan output) were easy: the tool
  output is categorical.
- The hard ones were the subtle numeric faults: null/volume/freshness/distribution drift, runtime
  anomalies, and embedding drift or corpus staleness close to the clean range. Two observations back this up:
  - On public, v1 missed 2 faults. v2 caught both: all 7 alerts v2 added had z between 2.0 and 2.7,
    so those 2 faults sit in the 2–3σ band, where v1's 2.5σ threshold and bare-bound warm-up missed them.
  - On private, moving from 2.5σ to 2.0σ added only ~1 catch. About a third of private faults still sit
    below 2σ, i.e. inside the clean distribution of a single metric. Every payload field was constant,
    so per-event metric values are the only signal, and a one-event test cannot separate those faults
    from clean noise without many more false alarms.

**What would you change about your cost/coverage tradeoff, if you had another pass?**
- **Threshold math.** A miss costs 0.5/n_faulty and a false alarm 0.3/n_clean. With ~3 clean events per
  faulty one, alerting pays off once the fault likelihood is above ~0.6× the clean likelihood, which is
  why v2 moved from 2.5σ to ~2σ. Going lower (1.6–1.8σ) tripled practice false alarms with no gain.
- **Temporal detectors.** Faults below 2σ need evidence across events: a per-metric CUSUM/EWMA, or a
  Bayesian per-event score that combines all metrics into one likelihood ratio, instead of max-z plus a
  χ² fallback.
- **Cost.** Single-pass coverage is right. On public, 9% overage costs ~1.8 pts, while skipping calls to
  stay under budget would lose more in missed faults (~1.3 pts each). On private (budget 320) cost was
  never binding.
