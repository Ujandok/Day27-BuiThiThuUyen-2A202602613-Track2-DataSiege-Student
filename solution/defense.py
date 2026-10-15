"""
Data Siege defense (v2).

Strategy
--------
* Exactly one metered call per event (the one tool that can see that event).
  Average cost ~1.5 credits/event: under budget on practice/private, ~9% over
  on public (cheaper than skipping calls, see reflection.md).
* Deterministic structural checks where the signal is categorical:
  contract violations, SLA breach, missing upstream edges, orphaned outputs.
* Statistical checks where the signal is numeric:
  - sigma comes from ctx.baseline (documented as clean mean +/- 3 sigma);
  - the clean centre starts from a prior (baseline midpoint, or the clean-
    pipeline mean measured on the labelled practice stream) and is blended
    with an online median of events judged clean, so the first events of a
    run are covered too (v1 had a warm-up blind spot here);
  - every known fault kind is directional (volume *spike*, null *spike*,
    freshness *lag*, distribution *shift up*, runtime *anomaly* = slower,
    embedding *drift*, corpus *staleness*, feature *skew*), so the test is
    one-sided in the fault direction at Z_UP, with a looser Z_DOWN check the
    other way in case a "drop" variant appears;
  - several moderately elevated metrics at once also alert (sum of squares).
* Thresholds come from the scoring formula: a miss costs 0.5/n_faulty, a
  false alarm 0.3/n_clean, so with ~3 clean events per faulty one we should
  alert whenever the fault likelihood exceeds ~0.6x the clean likelihood.
  That lands around 2 sigma, not the 3 sigma of the static baseline.
"""
import math
from api import Verdict

# ---------------------------------------------------------- tuning knobs
Z_UP = 2.0             # fault direction (higher = worse)
Z_DOWN = 3.0           # opposite direction (e.g. a volume drop)
COMBINED_SUMSQ = 8.0   # sum of squared positive z's across one event's metrics
PRIOR_WEIGHT = 8.0     # pseudo-count given to the prior centre vs online median
HIST_MAX = 200

# Clean-pipeline centres for metrics whose baseline only has an upper
# bound (mean + 3 sigma). Measured once as the mean of the *clean* events of
# the labelled practice stream. They are pipeline constants (like
# baselines.json), not per-event answers.
PRIOR_CENTRE = {
    "null_rate": 0.0044,
    "staleness": 4.13,
    "freshness_delay": 5.12,
    "lineage_duration": 4125.0,
    "centroid_shift": 0.0200,
    "doc_age": 26.4,
}
# metric -> (baseline key for lower bound or None, baseline key for upper bound)
BOUNDS = {
    "row_count": ("row_count_min", "row_count_max"),
    "mean_amount": ("mean_amount_min", "mean_amount_max"),
    "null_rate": (None, "null_rate_max"),
    "staleness": (None, "staleness_min_max"),
    "freshness_delay": (None, "freshness_delay_max_min"),
    "lineage_duration": (None, "lineage_duration_ms_max"),
    "centroid_shift": (None, "embedding_centroid_shift_max"),
    "doc_age": (None, "corpus_avg_doc_age_days_max"),
}
# Feature skew: clean |serve-train|/train_std is half-normal |N(0, s)| and the
# baseline is its mean + 3 std = (0.798 + 3 * 0.603) * s = 2.607 * s.
HALF_NORMAL_3SIGMA = 2.607


def register(ctx):
    ctx.state.setdefault("hist", {})
    ctx.state.setdefault("upstream", {})
    ctx.on("data_batch", check_data_batch)
    ctx.on("contract_checkpoint", check_contract_checkpoint)
    ctx.on("lineage_run", check_lineage_run)
    ctx.on("feature_materialization", check_feature_materialization)
    ctx.on("embedding_batch", check_embedding_batch)


# ---------------------------------------------------------------- helpers

def _median(values):
    # hand-rolled: the stdlib `statistics` module pulls in imports the sandbox blocks
    v = sorted(values)
    n = len(v)
    if n == 0:
        return 0.0
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


def _hist(ctx, name):
    return ctx.state.setdefault("hist", {}).setdefault(name, [])


def _remember(ctx, values):
    """Only values from events we judged clean feed the online statistics."""
    for name, x in values.items():
        if x is None:
            continue
        h = _hist(ctx, name)
        h.append(x)
        if len(h) > HIST_MAX:
            del h[0]


def _z(ctx, name, x):
    """Signed z-score of x for a baseline-backed metric (positive = fault direction)."""
    b = ctx.baseline
    lo_key, hi_key = BOUNDS[name]
    hi = b[hi_key]
    if lo_key is not None:
        lo = b[lo_key]
        prior = (lo + hi) / 2.0
        sigma = (hi - lo) / 6.0
    else:
        prior = PRIOR_CENTRE[name]
        sigma = (hi - prior) / 3.0
    h = _hist(ctx, name)
    centre = prior
    if h:
        n = float(len(h))
        centre = (PRIOR_WEIGHT * prior + n * _median(h)) / (PRIOR_WEIGHT + n)
    return (x - centre) / sigma if sigma > 0 else 0.0


def _z_online(ctx, name, x, warmup=8):
    """z-score against online robust stats only (metrics with no published bound)."""
    h = _hist(ctx, name)
    if len(h) < warmup:
        return 0.0
    mu = _median(h)
    mad = _median([abs(v - mu) for v in h]) * 1.4826
    return (x - mu) / mad if mad > 0 else 0.0


def _flag(z, label, value):
    if z > Z_UP:
        return f"{label}={value:g} high (z={z:.2f})"
    if z < -Z_DOWN:
        return f"{label}={value:g} low (z={z:.2f})"
    return None


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _verdict(pillar, reasons, score=0.0):
    if reasons:
        conf = min(0.99, 0.6 + 0.05 * score) if score else 0.95
        return Verdict(alert=True, confidence=round(conf, 3), reason="; ".join(reasons), pillar=pillar)
    return Verdict(alert=False, confidence=0.8, reason="within expected range", pillar=pillar)


def _bad(result):
    return not isinstance(result, dict) or "error" in result


def _combined(zs):
    return sum(max(z, 0.0) ** 2 for z in zs)


# ---------------------------------------------------------------- checks

def check_data_batch(payload, ctx):
    pillar = "checks"
    r = ctx.tools.batch_profile(payload.get("batch_id"))
    if _bad(r):
        return Verdict(alert=False, confidence=0.0, reason=f"tool error: {r}", pillar=pillar)
    metrics = {
        "row_count": _num(r.get("row_count")),
        "null_rate": _num((r.get("null_rate") or {}).get("customer_id")),
        "mean_amount": _num(r.get("mean_amount")),
        "staleness": _num(r.get("staleness_min")),
    }
    reasons, zs = [], []
    for name, x in metrics.items():
        if x is None:
            continue
        z = _z(ctx, name, x)
        zs.append(z)
        msg = _flag(z, name, x)
        if msg:
            reasons.append(msg)

    # spread has no published bound: only flag a clear change vs this run's history
    std = _num(r.get("std_amount"))
    if std is not None:
        z = _z_online(ctx, "std_amount", std)
        if abs(z) > 4.0:
            reasons.append(f"std_amount={std:g} spread change (z={z:.2f})")

    sumsq = _combined(zs)
    if not reasons and sumsq > COMBINED_SUMSQ:
        reasons.append(f"combined drift sum(z^2)={sumsq:.1f}")

    if not reasons:
        metrics["std_amount"] = std
        _remember(ctx, metrics)
    return _verdict(pillar, reasons, max((abs(z) for z in zs), default=0.0))


def check_contract_checkpoint(payload, ctx):
    pillar = "contracts"
    r = ctx.tools.contract_diff(payload.get("contract_id"), payload.get("checkpoint_batch_id"))
    if _bad(r):
        return Verdict(alert=False, confidence=0.0, reason=f"tool error: {r}", pillar=pillar)
    reasons = []
    violations = r.get("violations") or []
    if violations:
        reasons.append("violations: " + ",".join(map(str, violations)))

    delay = _num(r.get("freshness_delay_min"))
    sla = _num((payload.get("declared_sla") or {}).get("freshness_min"))
    z = 0.0
    if delay is not None:
        z = _z(ctx, "freshness_delay", delay)
        if sla is not None and delay > sla:
            reasons.append(f"SLA breach freshness_delay={delay:g} > declared {sla:g}")
        elif z > Z_UP:
            reasons.append(f"freshness_delay={delay:g} near SLA (z={z:.2f})")

    if not reasons:
        _remember(ctx, {"freshness_delay": delay})
    return _verdict(pillar, reasons, z)


def check_lineage_run(payload, ctx):
    pillar = "lineage"
    r = ctx.tools.lineage_graph_slice(payload.get("run_id"))
    if _bad(r):
        return Verdict(alert=False, confidence=0.0, reason=f"tool error: {r}", pillar=pillar)
    reasons = []
    job = payload.get("job", "?")
    actual_up = set(r.get("actual_upstream") or [])

    # 1) every declared input must actually be wired upstream
    declared = {i.get("name") for i in (payload.get("inputs") or []) if isinstance(i, dict)}
    missing_declared = declared - actual_up
    if missing_declared:
        reasons.append("missing declared upstream: " + ",".join(sorted(missing_declared)))

    # 2) upstream edges this job normally has (seen in >= half of its past runs)
    seen = ctx.state["upstream"].setdefault(job, {"runs": 0, "counts": {}})
    if seen["runs"] >= 2:
        usual = {u for u, c in seen["counts"].items() if c * 2 >= seen["runs"]}
        missing_usual = usual - actual_up - missing_declared
        if missing_usual:
            reasons.append("missing usual upstream: " + ",".join(sorted(missing_usual)))

    # 3) declared outputs with nobody consuming them
    outputs = payload.get("outputs") or []
    down = r.get("actual_downstream_count")
    if outputs and down is not None and down == 0:
        reasons.append("orphaned output (downstream_count=0)")

    # 4) runtime anomaly
    dur = _num(r.get("duration_ms"))
    z = 0.0
    if dur is not None:
        z = _z(ctx, "lineage_duration", dur)
        msg = _flag(z, "duration_ms", dur)
        if msg:
            reasons.append("runtime " + msg)

    # learn this job's normal graph shape from structurally clean runs
    if not missing_declared and not (outputs and down == 0):
        seen["runs"] += 1
        for u in actual_up:
            seen["counts"][u] = seen["counts"].get(u, 0) + 1
    if not reasons:
        _remember(ctx, {"lineage_duration": dur})
    return _verdict(pillar, reasons, abs(z))


def check_feature_materialization(payload, ctx):
    pillar = "ai_infra"
    r = ctx.tools.feature_drift(payload.get("feature_view"), payload.get("batch_id"))
    if _bad(r):
        return Verdict(alert=False, confidence=0.0, reason=f"tool error: {r}", pillar=pillar)
    serve = _num(r.get("serve_mean"))
    train = _num(r.get("train_mean"))
    tstd = _num(r.get("train_std"))
    if serve is not None and train is not None and tstd:
        d = (serve - train) / tstd               # signed shift in training sigmas
    else:
        d = _num(r.get("mean_shift_sigma"))      # unsigned fallback
    reasons, z = [], 0.0
    if d is not None:
        s = ctx.baseline["feature_mean_shift_sigma_max"] / HALF_NORMAL_3SIGMA
        z = d / s if s > 0 else 0.0
        if z > Z_UP or z < -Z_DOWN:
            reasons.append(f"train/serve skew shift={d:+.3f} train-sigma (z={z:.2f})")
    return _verdict(pillar, reasons, abs(z))


def check_embedding_batch(payload, ctx):
    pillar = "ai_infra"
    r = ctx.tools.embedding_drift(payload.get("corpus"), payload.get("chunk_batch_id"))
    if _bad(r):
        return Verdict(alert=False, confidence=0.0, reason=f"tool error: {r}", pillar=pillar)
    metrics = {
        "centroid_shift": _num(r.get("centroid_shift")),
        "doc_age": _num(r.get("avg_doc_age_days")),
    }
    reasons, zs = [], []
    for name, x in metrics.items():
        if x is None:
            continue
        z = _z(ctx, name, x)
        zs.append(z)
        if z > Z_UP:  # a *low* shift or a *fresh* corpus is never a fault
            reasons.append(f"{name}={x:g} high (z={z:.2f})")
    sumsq = _combined(zs)
    if not reasons and sumsq > COMBINED_SUMSQ * 0.75:
        reasons.append(f"combined embedding drift sum(z^2)={sumsq:.1f}")
    if not reasons:
        _remember(ctx, metrics)
    return _verdict(pillar, reasons, max(zs, default=0.0))
