"""ai_compute INDEPENDENT checker (separate arithmetic from the solver).

INDEPENDENCE DECLARATION
------------------------
This module is written from the RAW INPUT PACKET (JSON data) plus a SCHEDULE OUTPUT
file (treated as data under test). It does NOT import or call any computation routine
from ai_compute/model.py (LP solver), ai_compute/asap.py (greedy baseline), or
ai_compute/schema.py::compute_derived. All arithmetic below is re-implemented from
first principles:

  * own interval slicing from window start/end on the declared grid
  * own tariff-rate lookup by local wall-clock interval start
  * own per-job work / window / deadline / parallelism / capacity / grid-power /
    energy / cost re-derivation with closed-form arithmetic
  * own small-case enumeration (coarse GPU grid) as a second, structurally different
    method to corroborate the optimizer optimum when the instance is small enough
  * own feasibility verdict (PASS/FAIL with reasons)

Attribution: the EV module h/check/independent_check.py follows the same "raw inputs
only, no imports of the solver" pattern (attribution comment in MIGRATION_PLAN.md).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

TOL = 1e-6  # kWh / kW / GPU-hours
TOL_MONEY = 1e-6
PDIG = 12

# enumeration guard: only run the brute-force corroboration on tiny instances
_ENUM_MAX_INTERVALS = 6
_ENUM_MAX_JOBS = 2
_ENUM_GRID_STEP = 0.1  # GPU


# ------------------------------------------------------------------ own parsing

def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive timestamp rejected: {s}")
    return dt


def _to_iso(dt: datetime) -> str:
    off = dt.utcoffset()
    total_min = int(off.total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    ah, am = divmod(abs(total_min), 60)
    return dt.strftime(f"%Y-%m-%dT%H:%M:%S{sign}{ah:02d}:{am:02d}")


def _own_intervals(start: datetime, end: datetime,
                   interval_minutes: int) -> List[Tuple[datetime, datetime, float]]:
    """Own slicing of [start, end) into grid-aligned intervals."""
    if start >= end:
        raise ValueError("start >= end")
    if 60 % interval_minutes != 0:
        raise ValueError(f"interval_minutes must divide 60: {interval_minutes}")
    out = []
    t = start
    while t < end:
        nxt = min(t + timedelta(minutes=interval_minutes), end)
        h = (nxt - t).total_seconds() / 3600.0
        out.append((t, nxt, h))
        t = nxt
    return out


def _own_rate(tariff: Dict[str, Any], dt: datetime) -> float:
    """Own rate lookup: flat, or local wall-clock interval start inside a [start,end) row."""
    if tariff["kind"] == "flat":
        return float(tariff["rate_aud_per_kwh"])
    t = dt.hour * 60 + dt.minute
    for row in tariff["table"]:
        sh, sm = map(int, row["start"].split(":"))
        eh, em = map(int, row["end"].split(":"))
        s, e = sh * 60 + sm, eh * 60 + em
        if s <= t < e:
            return float(row["rate_aud_per_kwh"])
    raise ValueError(f"no tariff row covers local time {dt.strftime('%H:%M')}")


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ own derived quantities

def _own_derived(packet: Dict[str, Any]) -> Dict[str, Any]:
    site, tar, w = packet["site"], packet["tariff"], packet["window"]
    w_start, w_end = _parse_iso(w["start_iso"]), _parse_iso(w["end_iso"])
    imin = int(site["interval_minutes"])
    intervals = _own_intervals(w_start, w_end, imin)
    rates = [_own_rate(tar, s) for s, _, _ in intervals]
    jobs = []
    for j in packet["jobs"]:
        rel, dl = _parse_iso(j["release_iso"]), _parse_iso(j["deadline_iso"])
        jobs.append({
            "id": j["id"],
            "release": rel,
            "deadline": dl,
            "window_hours": (dl - rel).total_seconds() / 3600.0,
            "required_gpu_hours": float(j["required_gpu_hours"]),
            "max_parallel_gpu": float(j["max_parallel_gpu"]),
        })
    return {
        "currency": site["currency"],
        "interval_minutes": imin,
        "it_base_load_kw": float(site["it_base_load_kw"]),
        "incremental_kw_per_gpu": float(site["incremental_kw_per_gpu"]),
        "pue": float(site["pue"]),
        "grid_power_cap_kw": (None if site.get("grid_power_cap_kw") is None
                              else float(site["grid_power_cap_kw"])),
        "flex_compute_capacity_gpu": float(site["flex_compute_capacity_gpu"]),
        "budget": (None if site.get("budget") is None else float(site["budget"])),
        "intervals": intervals,
        "rates": rates,
        "jobs": jobs,
    }


# ------------------------------------------------------------------ own schedule evaluation

def _allocation_matrix(der: Dict[str, Any], out: Dict[str, Any]) -> List[Dict[str, float]]:
    """Extract per-interval allocation {job_id: x} from a schedule output (data under test)."""
    n = len(der["intervals"])
    alloc: List[Dict[str, float]] = [{} for _ in range(n)]
    for row in out.get("schedule", []):
        t = int(row["interval_index"])
        if 0 <= t < n:
            alloc[t] = {k: float(v) for k, v in row.get("allocations_gpu", {}).items()}
    return alloc


def evaluate(packet: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    """Independently recompute work/energy/cost from the schedule; returns verdict dict.

    Never calls the solver's functions: only packet + output-as-data.
    """
    der = _own_derived(packet)
    n = len(der["intervals"])
    alloc = _allocation_matrix(der, out)
    site = der
    incr, base, pue = (site["incremental_kw_per_gpu"], site["it_base_load_kw"], site["pue"])
    cap, flex = site["grid_power_cap_kw"], site["flex_compute_capacity_gpu"]
    currency = site["currency"]

    checks: List[str] = []
    issues: List[str] = []

    # ---- per-job work, window, parallelism ----
    per_job: Dict[str, Dict[str, float]] = {}
    for j in der["jobs"]:
        done = 0.0
        for t in range(n):
            s, e, h = der["intervals"][t]
            x = alloc[t].get(j["id"], 0.0)
            done += x * h
            # window: allocation must be inside [release, deadline)
            if x > TOL and not (j["release"] <= s and e <= j["deadline"]):
                issues.append(
                    f"job {j['id']}: allocation {x:.4f} GPU outside [release,deadline) at interval {t}"
                )
            if x > j["max_parallel_gpu"] + TOL:
                issues.append(
                    f"job {j['id']}: interval {t} allocation {x:.4f} > max_parallel {j['max_parallel_gpu']:.4f}"
                )
        req = j["required_gpu_hours"]
        if abs(done - req) > TOL:
            issues.append(f"job {j['id']}: work done {done:.6f} != required {req:.6f} GPU-hours")
        checks.append(
            f"job {j['id']}: required={req:.4f} GPU-h, done={done:.6f}, "
            f"window_hours={j['window_hours']:.4f}, max_parallel={j['max_parallel_gpu']:.4f} "
            f"-> {'OK' if abs(done - req) <= TOL else 'MISMATCH'}"
        )
        per_job[j["id"]] = {"done_gpu_hours": done, "required_gpu_hours": req,
                            "window_hours": j["window_hours"]}

    # ---- per-interval capacity + grid power + energy + cost (closed form) ----
    rows = []
    total_gpu_h = total_energy = total_cost = 0.0
    for t in range(n):
        s, e, h = der["intervals"][t]
        sumx = sum(alloc[t].values())
        if sumx > flex + TOL:
            issues.append(f"interval {t}: total allocation {sumx:.4f} > flex capacity {flex:.4f}")
        it_kw = base + incr * sumx
        grid_kw = pue * it_kw
        if cap is not None and grid_kw > cap + TOL:
            issues.append(
                f"interval {t}: grid power {grid_kw:.4f} kW > cap {cap:.4f} kW"
            )
        energy = grid_kw * h
        cost = der["rates"][t] * energy
        total_gpu_h += sumx * h
        total_energy += energy
        total_cost += cost
        rows.append({
            "interval_index": t,
            "start_iso": _to_iso(s),
            "end_iso": _to_iso(e),
            "interval_hours": round(h, PDIG),
            "total_allocation_gpu": round(sumx, PDIG),
            "it_power_kw": round(it_kw, PDIG),
            "grid_power_kw": round(grid_kw, PDIG),
            "grid_energy_kwh": round(energy, PDIG),
            "price_currency_per_kwh": der["rates"][t],
            "cost_currency": round(cost, PDIG),
        })
        checks.append(
            f"interval {t}: sumx={sumx:.4f} GPU, IT={it_kw:.4f} kW, grid={grid_kw:.4f} kW, "
            f"energy={energy:.4f} kWh, rate={der['rates'][t]:.4f}, cost={cost:.4f}"
        )

    # ---- budget headroom / overrun ----
    budget = site["budget"]
    headroom = None
    if budget is not None:
        headroom = budget - total_cost
        if headroom < -TOL_MONEY:
            issues.append(
                f"budget overrun: total cost {total_cost:.4f} {currency} > budget {budget:.4f} "
                f"({currency}, headroom {headroom:.4f})"
            )
        checks.append(
            f"budget: {budget:.4f} {currency}, headroom={headroom:.4f} "
            f"{'OK' if headroom >= -TOL_MONEY else 'OVERRUN'}"
        )

    status = out.get("status", "?")
    verdict = "PASS" if status == "feasible" and not issues else "FAIL"

    return {
        "verdict": verdict,
        "status_under_test": status,
        "currency": currency,
        "issues": issues,
        "checks": checks,
        "summary": {
            "total_gpu_hours": round(total_gpu_h, PDIG),
            "total_grid_energy_kwh": round(total_energy, PDIG),
            "total_cost_currency": round(total_cost, PDIG),
            "budget_headroom_currency": (round(headroom, PDIG) if headroom is not None else None),
        },
        "per_job": {k: {kk: round(vv, PDIG) for kk, vv in v.items()}
                    for k, v in per_job.items()},
        "schedule": rows,
    }


# ------------------------------------------------------------------ own enumeration corroboration

def enumerate_optimal(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Brute-force min-cost schedule over a coarse GPU grid for tiny instances.

    Structurally different from the LP solver (pure enumeration). Only runs
    when intervals <= _ENUM_MAX_INTERVALS and jobs <= _ENUM_MAX_JOBS; otherwise returns
    a skipped note (enumeration is a corroboration aid, not the product).
    """
    der = _own_derived(packet)
    n = len(der["intervals"])
    nj = len(der["jobs"])
    if n > _ENUM_MAX_INTERVALS or nj > _ENUM_MAX_JOBS:
        return {"mode": "enumeration", "skipped": True,
                "reason": f"instance too large: {n} intervals, {nj} jobs"}
    incr, base, pue = (der["incremental_kw_per_gpu"], der["it_base_load_kw"], der["pue"])
    cap, flex = der["grid_power_cap_kw"], der["flex_compute_capacity_gpu"]
    currency = der["currency"]

    # grid of possible x values per job per interval (0..min(max_parallel, flex), step)
    step = _ENUM_GRID_STEP
    grids = []
    for j in der["jobs"]:
        ub = min(j["max_parallel_gpu"], flex)
        vals = [round(k * step, 10) for k in range(0, int(ub / step) + 1)]
        if round(ub, 10) not in vals:
            vals.append(round(ub, 10))
        grids.append(vals)

    def in_window(j, t):
        s, e = der["intervals"][t][0], der["intervals"][t][1]
        return j["release"] <= s and e <= j["deadline"]

    best_cost = math.inf
    best_alloc = None
    # brute force over product of per-interval job allocation choices (tiny only)
    import itertools
    # build per-interval candidate tuples
    per_t = []
    for t in range(n):
        cand = [tuple(0.0 for _ in range(nj))]
        ranges = []
        for jj, j in enumerate(der["jobs"]):
            if in_window(j, t):
                ranges.append([(jj, v) for v in grids[jj]])
            else:
                ranges.append([(jj, 0.0)])
        for combo in itertools.product(*ranges):
            vec = [0.0] * nj
            for jj, v in combo:
                vec[jj] = v
            if sum(vec) <= flex + TOL:
                cand.append(tuple(round(v, 10) for v in vec))
        # dedupe
        per_t.append(list({c for c in cand}))

    for combo in itertools.product(*per_t):
        # work equality per job
        ok = True
        for jj, j in enumerate(der["jobs"]):
            done = sum(combo[t][jj] * der["intervals"][t][2] for t in range(n))
            if abs(done - j["required_gpu_hours"]) > 1e-3:  # coarse grid tolerance
                ok = False
                break
        if not ok:
            continue
        cost = 0.0
        for t in range(n):
            sumx = sum(combo[t])
            grid_kw = pue * (base + incr * sumx)
            if cap is not None and grid_kw > cap + TOL:
                ok = False
                break
            cost += der["rates"][t] * grid_kw * der["intervals"][t][2]
        if not ok:
            continue
        if cost < best_cost - 1e-9:
            best_cost = cost
            best_alloc = combo

    return {
        "mode": "enumeration",
        "skipped": False,
        "grid_step_gpu": step,
        "currency": currency,
        "best_cost_currency": (round(best_cost, PDIG) if best_cost < math.inf else None),
        "best_allocation": (
            [
                {der["jobs"][jj]["id"]: round(best_alloc[t][jj], 4)
                 for jj in range(nj) if best_alloc[t][jj] > 1e-9}
                for t in range(n)
            ]
            if best_alloc is not None else None
        ),
        "found_feasible": best_alloc is not None,
    }


# ------------------------------------------------------------------
# SCENARIO EVALUATION (independent arithmetic — Optimization workstream)
# ------------------------------------------------------------------
# Scores a frozen schedule under multiple declared price scenarios. Re-uses only
# the own-parsing/own-arithmetic helpers above; never imports the LP/greedy
# solvers. The scenario-rate list is data under test; feasibility/window/capacity
# checks are identical to evaluate() (scenario rates cannot change the allocation).

def _own_cost_with_rates(der, alloc_matrix, rates) -> tuple:
    """Own closed-form grid energy + cost for an allocation matrix and rate series."""
    incr, base, pue = (der["incremental_kw_per_gpu"], der["it_base_load_kw"], der["pue"])
    n = len(der["intervals"])
    if len(rates) != n:
        raise ValueError(f"rates length {len(rates)} != intervals {n}")
    total_energy = 0.0
    total_cost = 0.0
    for t in range(n):
        h = der["intervals"][t][2]
        sumx = sum(alloc_matrix[t].values())
        grid_kw = pue * (base + incr * sumx)
        e = grid_kw * h
        total_energy += e
        total_cost += rates[t] * e
    return total_energy, total_cost


def evaluate_scenarios(packet: Dict[str, Any], scenarios, out: Dict[str, Any]) -> Dict[str, Any]:
    """Score a frozen schedule under a list [(id, rates_aud_per_kwh)] of scenarios.

    Returns per-scenario cost (own arithmetic), min/mean/max exposure, and — when
    the output declares a worst-case cost (scenario worst-case LP) — an independent
    consistency check that the reported worst-case equals the recomputed max.
    """
    der = _own_derived(packet)
    n = len(der["intervals"])
    alloc = _allocation_matrix(der, out)
    rows = []
    for sid, rates in scenarios:
        energy, cost = _own_cost_with_rates(der, alloc, [float(r) for r in rates])
        rows.append({"scenario": str(sid), "grid_energy_kwh": round(energy, PDIG),
                     "cost_currency": round(cost, PDIG)})
    costs = [r["cost_currency"] for r in rows]
    exposure = {
        "n": len(costs),
        "min_cost_currency": round(min(costs), PDIG) if costs else None,
        "mean_cost_currency": round(sum(costs) / len(costs), PDIG) if costs else None,
        "max_cost_currency": round(max(costs), PDIG) if costs else None,
    }
    issues: List[str] = []
    reported_wc = out.get("summary", {}).get("worst_case_cost_currency")
    wc_match = None
    if reported_wc is not None:
        recomputed_max = exposure["max_cost_currency"]
        wc_match = abs(float(reported_wc) - float(recomputed_max)) <= 1e-6
        if not wc_match:
            issues.append(
                f"worst-case mismatch: reported {reported_wc:.8f} vs "
                f"recomputed max scenario cost {recomputed_max:.8f}"
            )
    return {
        "verdict": "PASS" if not issues else "FAIL",
        "issues": issues,
        "exposure": exposure,
        "reported_worst_case_consistent": wc_match,
        "scenario_costs": rows,
        "currency": der["currency"],
    }
