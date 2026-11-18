"""AI computing cost-minimizing schedule model (LP via CVXPY and HiGHS).

Model (units explicit):
  decision x[j,t] = average active-GPU equivalents for job j in interval t
  constraints:
    sum_t x[j,t] * interval_hours_t  == required_gpu_hours_j        (work equality)
    x[j,t] = 0 outside [release, deadline)                          (by upper bound)
    x[j,t] <= max_parallel_gpu_j                                    (per-job parallelism)
    sum_j x[j,t] <= flex_compute_capacity_gpu                       (total capacity)
    PUE * (it_base_load_kw + incremental_kw_per_gpu * sum_j x[j,t]) <= grid_power_cap_kw
  power / energy / cost:
    IT power   = it_base_load_kw + incremental_kw_per_gpu * sum_j x[j,t]     (kW)
    grid power = PUE * IT power                                             (kW)
    grid energy = grid power * interval_hours                                (kWh)
    cost        = price[t] * grid energy                                     (currency)
  objective: minimize sum_t price[t] * grid_power[t] * interval_hours_t

Preemptible/divisible only: x continuous in [0, ...]; no integer/non-preemptive claims.

Attribution: original LP formulation (minimize sum(rate*grid*hours), analytic infeas
pre-check + post guard) adapted from h/scheduler/core.py::schedule (EV scope).
"""
from __future__ import annotations

from typing import Any, Dict, List

import cvxpy as cp
import numpy as np

from .common import TOL_ENERGY, TOL_GPU, r12
from .schema import compute_derived, validate

SOLVER = "CVXPY / HiGHS"


def _feas_precheck(der: Dict[str, Any]) -> List[str]:
    """Analytic infeasibility pre-check, independent of the LP solver."""
    reasons: List[str] = []
    site = der["site"]
    intervals = der["intervals"]
    win_h = sum(h for _, _, h in intervals)
    incr, base, pue = site["incremental_kw_per_gpu"], site["it_base_load_kw"], site["pue"]
    cap = site["grid_power_cap_kw"]
    flex = site["flex_compute_capacity_gpu"]

    # effective per-interval capacity given grid cap
    cap_eff = flex
    if cap is not None:
        alloc_ub = (cap / pue - base) / incr if incr else None
        if alloc_ub is not None:
            cap_eff = min(cap_eff, alloc_ub)
            if alloc_ub <= 0:
                reasons.append(
                    f"grid cap too low: max allocation {(cap / pue - base):.4f} kW / "
                    f"{incr:.4f} kW-per-GPU = {alloc_ub:.4f} GPU <= 0"
                )

    for j in der["jobs"]:
        win_h_j = (j["deadline"] - j["release"]).total_seconds() / 3600.0
        max_work_j = j["max_parallel_gpu"] * win_h_j
        if j["required_gpu_hours"] > max_work_j + TOL_GPU:
            reasons.append(
                f"job {j['id']}: required_gpu_hours={j['required_gpu_hours']:.4f} > "
                f"max_parallel*window_hours={max_work_j:.4f}"
            )
    total_req = sum(j["required_gpu_hours"] for j in der["jobs"])
    if total_req > cap_eff * win_h + TOL_GPU:
        reasons.append(
            f"aggregate: total required GPU-hours {total_req:.4f} > "
            f"effective capacity {cap_eff:.4f} GPU * {win_h:.4f} h"
        )
    if cap is not None and pue * base > cap + TOL_ENERGY:
        reasons.append(
            f"grid cap below base load: PUE*base={pue * base:.4f} kW > cap={cap:.4f} kW"
        )
    return reasons


def schedule(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Run the LP scheduler on a validated packet; returns an output dict.

    Output shape:
      status: "feasible" | "infeasible"
      infeasibility_reasons: list of strings (analytic + solver)
      schedule: list of interval rows {interval_index, start_iso, end_iso,
                allocations_gpu {job_id: x}, total_allocation_gpu,
                it_power_kw, grid_power_kw, grid_energy_kwh, price, cost}
      summary: total GPU-hours, total grid energy kWh, total cost, budget headroom/overrun
      solver: "CVXPY / HiGHS"
    """
    errs = validate(packet)
    if errs:
        return _make_output(packet, "infeasible", ["validation: " + "; ".join(errs)], [], "n/a")

    der = compute_derived(packet)
    site, intervals, rates = der["site"], der["intervals"], der["rates_currency_per_kwh"]
    jobs = der["jobs"]
    n_t = len(intervals)
    incr, base, pue = site["incremental_kw_per_gpu"], site["it_base_load_kw"], site["pue"]
    cap, flex = site["grid_power_cap_kw"], site["flex_compute_capacity_gpu"]
    reasons = _feas_precheck(der)
    if reasons:
        return _make_output(packet, "infeasible", reasons, [], f"{SOLVER} (pre-check)")

    allowed = np.array([
        [job["release"] <= start and end <= job["deadline"] for start, end, _ in intervals]
        for job in jobs
    ])
    parallel_limits = np.array([min(job["max_parallel_gpu"], flex) for job in jobs])
    interval_hours = np.array([duration for _, _, duration in intervals])
    required_work = np.array([job["required_gpu_hours"] for job in jobs])
    allocation = cp.Variable((len(jobs), n_t), nonneg=True)
    total_gpus = cp.sum(allocation, axis=0)
    grid_power = pue * (base + incr * total_gpus)
    constraints = [
        allocation <= allowed * parallel_limits[:, None],
        allocation @ interval_hours == required_work,
        total_gpus <= flex,
    ]
    if cap is not None:
        constraints.append(grid_power <= cap)

    forecast_cost = cp.sum(cp.multiply(np.asarray(rates) * interval_hours, grid_power))
    problem = cp.Problem(cp.Minimize(forecast_cost), constraints)
    problem.solve(solver=cp.HIGHS)
    if problem.status != cp.OPTIMAL:
        reasons.append(f"solver_status: {problem.status}")
        return _make_output(packet, "infeasible", reasons, [], SOLVER)

    allocation_values = allocation.value

    # ---- build rows + post-guards ----
    rows: List[Dict[str, Any]] = []
    total_gpu_h = 0.0
    total_energy = 0.0
    total_cost = 0.0
    for t in range(n_t):
        alloc: Dict[str, float] = {}
        for job_index, job in enumerate(jobs):
            if allowed[job_index, t]:
                alloc[job["id"]] = max(0.0, float(allocation_values[job_index, t]))
        sumx = sum(alloc.values())
        it_kw = base + incr * sumx
        grid_kw = pue * it_kw
        hours = intervals[t][2]
        energy = grid_kw * hours
        cost = rates[t] * energy
        total_gpu_h += sumx * hours
        total_energy += energy
        total_cost += cost
        rows.append({
            "interval_index": t,
            "start_iso": _iso(intervals[t][0]),
            "end_iso": _iso(intervals[t][1]),
            "interval_hours": r12(hours),
            "allocations_gpu": {k: r12(v) for k, v in alloc.items()},
            "total_allocation_gpu": r12(sumx),
            "it_power_kw": r12(it_kw),
            "grid_power_kw": r12(grid_kw),
            "grid_energy_kwh": r12(energy),
            "price_currency_per_kwh": rates[t],
            "cost_currency": r12(cost),
        })

    # post guards
    for j in jobs:
        done = sum(rows[t]["allocations_gpu"].get(j["id"], 0.0) * rows[t]["interval_hours"]
                   for t in range(n_t))
        if abs(done - j["required_gpu_hours"]) > 1e-6:
            reasons.append(
                f"post_solve_check_failed: job {j['id']} done={done:.6f} "
                f"!= required={j['required_gpu_hours']:.6f}"
            )
    for t in range(n_t):
        if rows[t]["total_allocation_gpu"] > flex + 1e-6:
            reasons.append(f"post_solve_check_failed: interval {t} exceeds flex capacity")
    if cap is not None:
        for t in range(n_t):
            if rows[t]["grid_power_kw"] > cap + 1e-6:
                reasons.append(f"post_solve_check_failed: interval {t} grid power exceeds cap")

    if reasons:
        return _make_output(packet, "infeasible", reasons, rows, f"{SOLVER} (post-guard)")

    return _make_output(packet, "feasible", [], rows, SOLVER,
                        total_gpu_h, total_energy, total_cost)


# ---------------------------------------------------------------- helpers

def _iso(dt) -> str:
    from .common import to_iso
    return to_iso(dt)


def _make_output(packet: Dict[str, Any], status: str, reasons: List[str],
                 rows: List[Dict[str, Any]], solver: str,
                 total_gpu_h: float = 0.0, total_energy: float = 0.0,
                 total_cost: float = 0.0) -> Dict[str, Any]:
    site = packet.get("site", {})
    budget = site.get("budget")
    headroom = None
    if budget is not None and status == "feasible":
        headroom = float(budget) - total_cost
    return {
        "schema_version": "1.0",
        "label": packet.get("label", ""),
        "mode": "optimizer",
        "status": status,
        "infeasibility_reasons": reasons,
        "schedule": rows,
        "summary": {
            "total_gpu_hours": r12(total_gpu_h),
            "total_grid_energy_kwh": r12(total_energy),
            "total_cost_currency": r12(total_cost),
            "currency": site.get("currency", ""),
            "budget_headroom_currency": (r12(headroom) if headroom is not None else None),
        },
        "meta": {
            "tool": "ai_compute optimizer v1.0",
            "solver": solver,
            "model": "preemptible/divisible LP, x[j,t] avg active-GPU equivalents",
            "label_synthetic": packet.get("synthetic", False),
        },
    }
