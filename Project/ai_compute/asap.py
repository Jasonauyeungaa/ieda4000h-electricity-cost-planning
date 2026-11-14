"""ai_compute ASAP baseline scheduler (work-conserving greedy).

Rule: process intervals in chronological order; in each interval, allocate available
capacity to the released-and-unfinished jobs in ascending order of
  (deadline, job id)   <- documented tie rule: earliest deadline first, then job id.
A job's allocation is capped by min(max_parallel_gpu, remaining GPU-hours / hours).
This is work-conserving: capacity is never left idle while any eligible job has
unfinished work. Uses the SAME packet as the optimizer (identical jobs/capacities/
info/completed-work), so cost comparisons are apples-to-apples.

If the window ends with unfinished work, it returns status "infeasible" and reports
the work shortfall (GPU-hours unprocessed) and the deadline shortfall separately —
it never pretends a partial schedule beat a complete one.

Attribution: greedy baseline pattern (same input as optimizer, exact last-interval
scaling, infeasible reporting) adapted from h/scheduler/core.py::charge_on_arrival
(EV scope) — semantics adjusted for multi-job GPU-workload scheduling.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .common import TOL_GPU, r12, to_iso
from .schema import compute_derived, validate


def asap_schedule(packet: Dict[str, Any]) -> Dict[str, Any]:
    errs = validate(packet)
    reasons: List[str] = []
    if errs:
        reasons.append("validation: " + "; ".join(errs))
    try:
        der = compute_derived(packet)
    except Exception as e:  # structural problem (bad table etc.) — cannot even run greedy
        return _out(packet, "infeasible", reasons + [f"cannot_parse: {e}"], [], 0.0, 0.0, 0.0,
                    shortfall_gpu_hours=None, late_jobs=[])

    site, intervals, rates = der["site"], der["intervals"], der["rates_currency_per_kwh"]
    jobs = der["jobs"]
    n_t = len(intervals)
    incr, base, pue = site["incremental_kw_per_gpu"], site["it_base_load_kw"], site["pue"]
    cap, flex = site["grid_power_cap_kw"], site["flex_compute_capacity_gpu"]
    currency = site["currency"]

    # effective per-interval capacity given grid cap
    cap_eff = flex
    if cap is not None:
        alloc_ub = (cap / pue - base) / incr if incr else None
        if alloc_ub is not None:
            cap_eff = min(cap_eff, alloc_ub)

    remaining = {j["id"]: j["required_gpu_hours"] for j in jobs}
    rows: List[Dict[str, Any]] = []
    total_gpu_h = total_energy = total_cost = 0.0

    for t in range(n_t):
        s, e, hours = intervals[t]
        alloc: Dict[str, float] = {}
        # eligible: released, deadline not passed (window strictly inside), unfinished
        eligible = [
            j for j in jobs
            if j["release"] <= s and e <= j["deadline"]
            and remaining[j["id"]] > TOL_GPU
        ]
        # tie rule: earliest deadline then job id
        eligible.sort(key=lambda j: (j["deadline"], j["id"]))
        avail = cap_eff
        for j in eligible:
            if avail <= TOL_GPU:
                break
            take = min(avail, remaining[j["id"]] / hours, j["max_parallel_gpu"])
            if take <= TOL_GPU:
                continue
            alloc[j["id"]] = take
            remaining[j["id"]] -= take * hours
            avail -= take

        sumx = sum(alloc.values())
        it_kw = base + incr * sumx
        grid_kw = pue * it_kw
        energy = grid_kw * hours
        cost = rates[t] * energy
        total_gpu_h += sumx * hours
        total_energy += energy
        total_cost += cost
        rows.append({
            "interval_index": t,
            "start_iso": to_iso(s),
            "end_iso": to_iso(e),
            "interval_hours": r12(hours),
            "allocations_gpu": {k: r12(v) for k, v in alloc.items()},
            "total_allocation_gpu": r12(sumx),
            "it_power_kw": r12(it_kw),
            "grid_power_kw": r12(grid_kw),
            "grid_energy_kwh": r12(energy),
            "price_currency_per_kwh": rates[t],
            "cost_currency": r12(cost),
        })

    # shortfall reporting (separate from any complete-schedule comparison)
    shortfall = sum(max(0.0, v) for v in remaining.values())
    late_jobs = [j["id"] for j in jobs if remaining[j["id"]] > TOL_GPU]
    reasons: List[str] = []
    if shortfall > TOL_GPU:
        reasons.append(
            "deadline_shortfall: window ended with unfinished work "
            f"(GPU-hours unprocessed={shortfall:.6f}); jobs late={late_jobs}"
        )
        return _out(packet, "infeasible", reasons, rows, total_gpu_h, total_energy, total_cost,
                    shortfall_gpu_hours=shortfall, late_jobs=late_jobs)

    return _out(packet, "feasible", [], rows, total_gpu_h, total_energy, total_cost,
                shortfall_gpu_hours=0.0, late_jobs=[])


def _out(packet: Dict[str, Any], status: str, reasons: List[str],
         rows: List[Dict[str, Any]], total_gpu_h: float, total_energy: float,
         total_cost: float, shortfall_gpu_hours: float | None, late_jobs: List[str]) -> Dict[str, Any]:
    site = packet.get("site", {})
    budget = site.get("budget")
    headroom = None
    if budget is not None and status == "feasible":
        headroom = float(budget) - total_cost
    return {
        "schema_version": "1.0",
        "label": packet.get("label", ""),
        "mode": "asap",
        "status": status,
        "infeasibility_reasons": reasons,
        "schedule": rows,
        "summary": {
            "total_gpu_hours": r12(total_gpu_h),
            "total_grid_energy_kwh": r12(total_energy),
            "total_cost_currency": r12(total_cost),
            "currency": site.get("currency", ""),
            "budget_headroom_currency": (r12(headroom) if headroom is not None else None),
            "shortfall_gpu_hours": r12(shortfall_gpu_hours) if shortfall_gpu_hours is not None else None,
            "late_jobs": late_jobs,
        },
        "meta": {
            "tool": "ai_compute ASAP v1.0",
            "solver": "greedy (work-conserving, tie=earliest deadline then job id)",
            "label_synthetic": packet.get("synthetic", False),
        },
    }
