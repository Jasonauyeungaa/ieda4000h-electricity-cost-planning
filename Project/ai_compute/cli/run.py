"""ai_compute CLI — run baseline + optimizer + independent checker on a workload packet.

USAGE (from implementations/swarm/; project venv only):
    ./.venv/bin/python -m ai_compute.cli.run <packet.json> [--out <output_dir>]

What it does, in order:
  1. read the contract/workload packet (JSON, schema_version "1.0")
  2. validate it (schema.validate): tariffs, windows, jobs, units, feasibility
  3. run the ASAP baseline (work-conserving greedy, tie = earliest deadline then job id)
  4. run the cost-minimizing LP optimizer (pulp-CBC, preemptible/divisible model)
  5. run the INDEPENDENT checker (checker.evaluate) on BOTH schedules; this
     recomputes work/energy/cost from scratch (it never imports the solver code)
  6. run brute-force enumeration corroboration when the instance is tiny
  7. report energy / cost / budget headroom / deadline-capacity checks / sensitivity
     (rate scales x0.9/x1.0/x1.1 + flat-equivalent average rate)
  8. write <out>/result.json (structured) and <out>/summary.txt (human-readable);
     the same summary is printed to stdout

Exit codes (documented):
  0  pipeline completed and the schedule(s) are feasible with checker verdict PASS
  2  input packet failed validation (nothing scheduled)
  3  pipeline completed but the schedule(s) are infeasible or the checker FAILed
  1  unexpected internal error

SYNTHETIC policy: packet.synthetic=true stays visible in result.json/meta and in the
summary header. This tool makes NO real-savings claims — results only illustrate the
scheduler mechanics on the given inputs.

Attribution: CLI skeleton/human-summary conventions adapted from
implementations/swarm/h/run_benchmark.py (EV scope) — see MIGRATION_PLAN.md reuse table.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from ..common import TOL_GPU, read_json, write_json, r12, SCHEMA_VERSION, to_iso
from ..schema import validate
from ..asap import asap_schedule
from ..model import schedule as lp_schedule
from ..checker import evaluate, enumerate_optimal

TOOL = "ai_compute-cli v1.0"
RATE_SCALES = (0.9, 1.0, 1.1)          # sensitivity: optimum cost under scaled rates
CURR_TOL = 1e-6


def _fmt(x: Optional[float], nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _active_intervals(out: Dict[str, Any]) -> List[int]:
    return [r["interval_index"] for r in out.get("schedule", [])
            if float(r.get("total_allocation_gpu", 0.0)) > TOL_GPU]


def _deadline_capacity_checks(packet: Dict[str, Any],
                              out: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Self-contained deadline/capacity audit of one schedule output (not the checker)."""
    site, jobs = packet["site"], packet.get("jobs", [])
    cap = site.get("grid_power_cap_kw")
    flex = float(site["flex_compute_capacity_gpu"])
    rows = out.get("schedule", [])
    checks: List[Dict[str, Any]] = []
    max_alloc = max((float(r.get("total_allocation_gpu", 0.0)) for r in rows), default=0.0)
    checks.append({
        "check": "flex_capacity",
        "ok": max_alloc <= flex + CURR_TOL,
        "detail": (f"max total allocation {max_alloc:.4f} GPU <= flex {flex:.4f} GPU"),
    })
    if cap is not None:
        max_grid = max((float(r.get("grid_power_kw", 0.0)) for r in rows), default=0.0)
        checks.append({
            "check": "grid_power_cap",
            "ok": max_grid <= float(cap) + CURR_TOL,
            "detail": (f"max grid power {max_grid:.4f} kW <= cap {float(cap):.4f} kW"),
        })
    if out.get("status") == "feasible":
        deadline_ok = True
    else:
        deadline_ok = False
    late = out.get("summary", {}).get("late_jobs", [])
    short = out.get("summary", {}).get("shortfall_gpu_hours")
    detail = ("all jobs finished inside [release, deadline)"
              if deadline_ok else
              f"shortfall {_fmt(short, 6)} GPU-hours; late jobs {late or 'n/a'}")
    checks.append({"check": "deadlines", "ok": deadline_ok, "detail": detail})
    return checks


def _sensitivity(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Rates-only sensitivity: re-run the LP optimum with the tariff scaled."""
    sens: Dict[str, Any] = {"rate_scales": list(RATE_SCALES), "opt_costs_currency": {}}
    base_cost = None
    for f in RATE_SCALES:
        scaled = copy.deepcopy(packet)
        tar = scaled["tariff"]
        if tar["kind"] == "flat":
            tar["rate_aud_per_kwh"] = round(float(tar["rate_aud_per_kwh"]) * f, 12)
        else:
            for row in tar["table"]:
                row["rate_aud_per_kwh"] = round(float(row["rate_aud_per_kwh"]) * f, 12)
        out = lp_schedule(scaled)
        c = out.get("summary", {}).get("total_cost_currency")
        sens["opt_costs_currency"][str(f)] = (r12(c) if c is not None else None)
        if abs(f - 1.0) < 1e-9:
            base_cost = c
    # flat-equivalent average rate = cost / total grid energy (declared currency per kWh)
    flat = lp_schedule(packet)
    e = flat.get("summary", {}).get("total_grid_energy_kwh")
    sens["flat_equiv_avg_rate_currency_per_kwh"] = (
        r12(base_cost / e) if base_cost is not None and e else None)
    return sens


def _build_summary(packet: Dict[str, Any], validation: List[str],
                   asap_out: Dict[str, Any], opt_out: Dict[str, Any],
                   chk_asap: Dict[str, Any], chk_opt: Dict[str, Any],
                   enum: Dict[str, Any], sens: Dict[str, Any],
                   out_dir: str, exit_code: int) -> str:
    L: List[str] = []
    a = L.append
    site, tar = packet["site"], packet["tariff"]
    cur = site["currency"]
    a("=" * 78)
    a(f"ai_compute CLI — schedule run  (input is {'SYNTHETIC' if packet.get('synthetic') else 'UNLABELED'})")
    a("=" * 78)
    a(f"label        : {packet.get('label','')}")
    a(f"packet file  : {packet.get('_source_file','') or 'n/a'}")
    a(f"schema       : {packet.get('schema_version','?')} | tool {TOOL}")
    a("")
    a("INPUT")
    w = packet["window"]
    a(f"  window     : {w['start_iso']} -> {w['end_iso']} "
      f"({len(asap_out.get('schedule', []))} x "
      f"{asap_out['schedule'][0]['interval_hours'] if asap_out.get('schedule') else '?'} h)")
    a(f"  site       : {site['id']} | {cur} | base {site['it_base_load_kw']} kW | "
      f"+{site['incremental_kw_per_gpu']} kW/GPU | PUE {site['pue']} | "
      f"flex {site['flex_compute_capacity_gpu']} GPU | "
      f"grid cap {site.get('grid_power_cap_kw', 'n/a')} kW | "
      f"budget {site.get('budget', 'n/a')} {cur}")
    if tar["kind"] == "flat":
        a(f"  tariff     : flat {tar['rate_aud_per_kwh']} {cur}/kWh")
    else:
        rows = " | ".join(f"{r['start']}-{r['end']} {r['rate_aud_per_kwh']}" for r in tar["table"])
        a(f"  tariff     : dynamic ({rows}) {cur}/kWh")
    a("  jobs")
    for j in packet["jobs"]:
        a(f"    {j['id']}: {j['required_gpu_hours']} GPU-h, max-parallel {j['max_parallel_gpu']}, "
          f"release {j['release_iso']} -> deadline {j['deadline_iso']}")
    a(f"  validation : {'PASS' if not validation else 'FAIL: ' + '; '.join(validation)}")
    a("")
    for tag, out, chk in (("ASAP BASELINE (work-conserving, tie=deadline then id)", asap_out, chk_asap),
                          ("OPTIMIZER (pulp-CBC, min-cost LP)", opt_out, chk_opt)):
        s = out["summary"]
        active = _active_intervals(out)
        a(tag)
        a(f"  status    : {out['status']}"
          + (f" | reasons: {'; '.join(out.get('infeasibility_reasons', []))}"
             if out.get('infeasibility_reasons') else ""))
        a(f"  active    : intervals {active}")
        a(f"  totals    : {s['total_gpu_hours']} GPU-h | {s['total_grid_energy_kwh']} kWh | "
          f"{s['total_cost_currency']} {cur}")
        a(f"  budget    : headroom {_fmt(s.get('budget_headroom_currency'), 4)} {cur}")
        a(f"  checker   : {chk['verdict']}"
          + (f" | issues: {'; '.join(chk['issues'])}" if chk.get('issues') else " | issues: none"))
        a("")
    enum_note = ("(skipped: instance too large)" if enum.get("skipped") else
                 (f"best {_fmt(enum.get('best_cost_currency'), 4)} {cur}, "
                  f"grid step {enum.get('grid_step_gpu')} GPU"
                  if enum.get("found_feasible") else "no feasible point found on grid"))
    a(f"ENUMERATION corroboration : {enum_note}")
    a("")
    asap_c = asap_out["summary"].get("total_cost_currency")
    opt_c = opt_out["summary"].get("total_cost_currency")
    if asap_out["status"] == "feasible" and opt_out["status"] == "feasible" \
            and asap_c is not None and opt_c is not None:
        a(f"saving (ASAP - OPT)       : {max(0.0, asap_c - opt_c):.4f} {cur} "
          f"({(asap_c - opt_c) / asap_c * 100.0 if asap_c else 0.0:.1f}%)")
    a("deadline / capacity (audited from schedules):")
    done: Dict[str, bool] = {}
    for chk in _deadline_capacity_checks(packet, opt_out):
        a(f"  - {chk['check']:16s}: {'OK' if chk['ok'] else 'VIOLATION'}  {chk['detail']}")
        done[chk["check"]] = chk["ok"]
    a("")
    a("SENSITIVITY (rates-only; structure unchanged)")
    for f, c in sens["opt_costs_currency"].items():
        a(f"  rate x{f:<4}: optimum cost {_fmt(c, 4)} {cur}"
          + (f"   ({((c - opt_c) / opt_c * 100.0) if opt_c and c is not None else 0.0:+.1f}%)"
             if opt_c else ""))
    a(f"  flat-equiv avg rate     : {_fmt(sens.get('flat_equiv_avg_rate_currency_per_kwh'), 4)} {cur}/kWh")
    a("")
    a("RESULT FILES")
    a(f"  {os.path.join(out_dir, 'result.json')}")
    a(f"  {os.path.join(out_dir, 'summary.txt')}")
    a("")
    a(f"VERDICT: input {'valid' if not validation else 'INVALID'} | "
      f"schedules {'feasible' if (asap_out['status'] == 'feasible' or opt_out['status'] == 'feasible') else 'infeasible'} | "
      f"checker {'PASS' if (chk_asap['verdict'] == 'PASS' or chk_opt['verdict'] == 'PASS') else 'FAIL'} | "
      f"exit code {exit_code}")
    a("NOTE: inputs are SYNTHETIC (unless labeled otherwise); results illustrate the "
      "scheduler mechanics and are NOT a claim of real savings for any company.")
    a("=" * 78)
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m ai_compute.cli.run",
        description="ai_compute CLI: ASAP baseline + LP optimizer + independent checker "
                    "on a workload packet (see module docstring for full documentation).")
    p.add_argument("packet", help="path to the workload packet JSON (schema_version 1.0)")
    p.add_argument("--out", default=None,
                   help="output directory (default: <ai_compute>/results/cli_<label>)")
    args = p.parse_args(argv)

    try:
        packet = read_json(args.packet)
        if not isinstance(packet, dict):
            print("input error: packet JSON must be an object", file=sys.stderr)
            return 2
        packet["_source_file"] = os.path.abspath(args.packet)
    except Exception as e:
        print(f"input error: cannot read {args.packet}: {e}", file=sys.stderr)
        return 2

    validation = validate(packet)
    if validation:
        print("VALIDATION FAILED:", file=sys.stderr)
        for e in validation:
            print(f"  - {e}", file=sys.stderr)
        out_dir = args.out or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "results", f"cli_{packet.get('label', 'invalid')}")
        os.makedirs(out_dir, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "label": packet.get("label", ""),
                   "synthetic": bool(packet.get("synthetic")), "input_file": args.packet,
                   "validation": validation, "status": "invalid_input",
                   "exit_code": 2}
        write_json(os.path.join(out_dir, "result.json"), payload)
        return 2

    try:
        asap_out = asap_schedule(packet)
        opt_out = lp_schedule(packet)
        chk_asap = evaluate(packet, asap_out)
        chk_opt = evaluate(packet, opt_out)
        enum = enumerate_optimal(packet)
        sens = _sensitivity(packet)
    except Exception as e:
        print(f"internal error: {e}", file=sys.stderr)
        return 1

    ok_feasible = (asap_out["status"] == "feasible" and chk_asap["verdict"] == "PASS"
                   and opt_out["status"] == "feasible" and chk_opt["verdict"] == "PASS")
    exit_code = 0 if ok_feasible else 3

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "results", f"cli_{packet.get('label', 'run')}")
    os.makedirs(out_dir, exist_ok=True)

    result = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "label": packet["label"],
        "synthetic": bool(packet.get("synthetic")),
        "input_file": os.path.abspath(args.packet),
        "validation": {"errors": validation, "passed": True},
        "asap": asap_out,
        "optimizer": opt_out,
        "checker": {"asap": chk_asap, "optimizer": chk_opt},
        "enumeration": enum,
        "sensitivity": sens,
        "deadline_capacity_checks": {
            "asap": _deadline_capacity_checks(packet, asap_out),
            "optimizer": _deadline_capacity_checks(packet, opt_out),
        },
        "exit_code": exit_code,
    }
    summary_txt = _build_summary(packet, validation, asap_out, opt_out,
                                 chk_asap, chk_opt, enum, sens, out_dir, exit_code)

    write_json(os.path.join(out_dir, "result.json"), result)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary_txt + "\n")
    print(summary_txt)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())