"""ai_compute cost-minimizing schedule model (LP via pulp).

Model (units explicit):
  decision x[j,t] = average active-GPU equivalents for job j in interval t
  constraints:
    sum_t x[j,t] * interval_hours_t  == required_gpu_hours_j        (work equality)
    x[j,t] = 0 outside [release, deadline)                          (by variable set)
    x[j,t] <= max_parallel_gpu_j                                    (per-job parallelism)
    sum_j x[j,t] <= flex_compute_capacity_gpu                       (total capacity)
    PUE * (it_base_load_kw + incremental_kw_per_gpu * sum_j x[j,t]) <= grid_power_cap_kw
  power / energy / cost:
    IT power   = it_base_load_kw + incremental_kw_per_gpu * sum_j x[j,t]     (kW)
    grid power = PUE * IT power                                             (kW)
    grid energy = grid power * interval_hours                                (kWh)
    cost        = price[t] * grid energy                                     (currency)
  objective: minimize sum_t price[t] * grid_power[t] * interval_hours_t
  (constant base-load term dropped from objective — it does not change argmin)

Preemptible/divisible only: x continuous in [0, ...]; no integer/non-preemptive claims.

Attribution: LP pattern (pulp-CBC, minimize sum(rate*grid*hours), analytic infeas
pre-check + post guard) adapted from h/scheduler/core.py::schedule (EV scope).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pulp

from .common import TOL_ENERGY, TOL_GPU, TOL_MONEY, r12
from .schema import compute_derived, validate


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
      solver: "pulp-CBC"
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
    currency = site["currency"]

    reasons = _feas_precheck(der)
    if reasons:
        return _make_output(packet, "infeasible", reasons, [], "pulp-CBC (pre-check)")

    # per-job allowed interval set: t inside [release, deadline)
    def in_window(j: Dict[str, Any], t: int) -> bool:
        s, e = intervals[t][0], intervals[t][1]
        return j["release"] <= s and e <= j["deadline"]  # end may touch deadline

    # ---- LP ----
    prob = pulp.LpProblem("ai_compute_min_cost", pulp.LpMinimize)
    x: Dict[Tuple[str, int], pulp.LpVariable] = {}
    for j in jobs:
        for t in range(n_t):
            if not in_window(j, t):
                continue
            ub = min(j["max_parallel_gpu"], flex)
            x[(j["id"], t)] = pulp.LpVariable(f"x_{j['id']}_{t}", lowBound=0, upBound=ub)

    # work equality per job
    for j in jobs:
        prob += (
            pulp.lpSum(x[(j["id"], t)] * intervals[t][2]
                       for t in range(n_t) if (j["id"], t) in x)
            == j["required_gpu_hours"]
        )
    # total capacity per interval
    for t in range(n_t):
        prob += (
            pulp.lpSum(x[(j["id"], t)] for j in jobs if (j["id"], t) in x)
            <= flex
        )
    # grid cap per interval: PUE*(base + incr*sumx) <= cap  (only if cap set)
    if cap is not None:
        for t in range(n_t):
            prob += (
                pue * (base + incr * pulp.lpSum(x[(j["id"], t)] for j in jobs if (j["id"], t) in x))
                <= cap
            )

    # objective: minimize sum price*PUE*(base + incr*sumx)*hours  (base constant, keep for clarity)
    obj_terms = []
    for t in range(n_t):
        sumx = pulp.lpSum(x[(j["id"], t)] for j in jobs if (j["id"], t) in x)
        grid_kw = pue * (base + incr * sumx)
        obj_terms.append(rates[t] * grid_kw * intervals[t][2])
    prob += pulp.lpSum(obj_terms)

    solver = pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)
    if pulp.LpStatus[status] != "Optimal":
        reasons.append(f"solver_status: {pulp.LpStatus[status]}")
        return _make_output(packet, "infeasible", reasons, [], "pulp-CBC")

    # ---- build rows + post-guards ----
    rows: List[Dict[str, Any]] = []
    total_gpu_h = 0.0
    total_energy = 0.0
    total_cost = 0.0
    for t in range(n_t):
        alloc: Dict[str, float] = {}
        for j in jobs:
            if (j["id"], t) in x:
                alloc[j["id"]] = max(0.0, float(x[(j["id"], t)].value()))
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
        return _make_output(packet, "infeasible", reasons, rows, "pulp-CBC (post-guard)")

    return _make_output(packet, "feasible", [], rows, "pulp-CBC",
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
