"""ai_compute workload/job schema + validator (AI-company scope).

Units contract (explicit everywhere):
  - required GPU-hours        : gpu_hours
  - max parallel GPU          : active-GPU equivalents (dimensionless count)
  - flexible compute capacity : active-GPU equivalents
  - incremental kW per GPU    : kW per active-GPU
  - IT base load              : kW (non-deferrable)
  - PUE                        : dimensionless, >= 1
  - grid power cap             : kW
  - price / rate               : currency per kWh
  - interval_minutes           : minutes, must divide 60

Validation flags: missing/conflicting terms, wrong units, stale/expired dates,
infeasible windows, negative values -> list of error strings (empty = valid).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .common import SCHEMA_VERSION, TOL_GPU, ContractError, build_intervals, parse_iso

_TOL_DT = timedelta(seconds=1)  # 1s boundary tolerance for job windows


# ---------------------------------------------------------------- tariff table

def _row_to_min(row: Dict[str, Any]) -> tuple:
    s_h, s_m = map(int, row["start"].split(":"))
    e_h, e_m = map(int, row["end"].split(":"))
    return s_h * 60 + s_m, e_h * 60 + e_m, float(row["rate_aud_per_kwh"])


def validate_tariff_table(table: List[Dict[str, Any]],
                         w_start: Optional[datetime] = None,
                         w_end: Optional[datetime] = None) -> List[str]:
    """Dynamic tariff rows must cover the schedule window contiguously (no gaps/overlaps),
    rates > 0. If no window is given, the table must tile a full 24h day.

    Mirrors h/scheduler/common.py::validate_tariff_table (attribution in common.py),
    generalized to window-relative coverage for partial-day contracts.
    Returns list of error strings (empty = ok).
    """
    errs: List[str] = []
    try:
        rows = sorted(table, key=lambda r: (int(r["start"][:2]), int(r["start"][3:5])))
    except (KeyError, TypeError, ValueError):
        return ["tariff: table rows must have 'start'/'end' HH:MM strings"]
    parsed = []
    for row in rows:
        try:
            s, e, rate = _row_to_min(row)
        except (KeyError, TypeError, ValueError):
            errs.append(f"tariff: malformed row {row!r}")
            continue
        if e <= s:
            errs.append(f"tariff: row must have end > start: {row}")
        if e > 24 * 60:
            errs.append(f"tariff: row end must be <= 24:00: {row}")
        if rate <= 0:
            errs.append(f"tariff: rate must be > 0: {row}")
        parsed.append((s, e, rate))

    if w_start is not None and w_end is not None:
        # window-relative coverage (handles windows that cross midnight)
        win_len = int((w_end - w_start).total_seconds() // 60)
        start_min = w_start.hour * 60 + w_start.minute
        need = {(start_min + k) % 1440 for k in range(win_len)}
        seen = set()
        overlap_first: Optional[int] = None
        overlap_count = 0
        for s, e, rate in parsed:
            for m in range(s, e):
                if m in need:
                    if m in seen:
                        if overlap_first is None:
                            overlap_first = m
                        overlap_count += 1
                    seen.add(m)
        if overlap_first is not None:
            errs.append(
                f"tariff: overlapping coverage at {overlap_first // 60:02d}:{overlap_first % 60:02d} "
                f"(total {overlap_count} overlapping minute(s) in window)"
            )
        missing = sorted(need - seen)
        if missing:
            errs.append(
                f"tariff: window not fully covered; first uncovered "
                f"{missing[0] // 60:02d}:{missing[0] % 60:02d}"
            )
    else:
        cursor = 0
        for s, e, rate in parsed:
            if s != cursor:
                errs.append(f"tariff: gap/overlap at {cursor // 60:02d}:{cursor % 60:02d}")
            cursor = e
        if cursor != 24 * 60:
            errs.append(f"tariff: table must end at 24:00, ended at {cursor // 60:02d}:{cursor % 60:02d}")
    return errs


# ---------------------------------------------------------------- helpers

def _num(x: Any, lo: float, hi: Optional[float] = None) -> Optional[float]:
    """Coerce to float and range-check; None if invalid (bool rejected)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


# ---------------------------------------------------------------- main validation

def validate(packet: Dict[str, Any]) -> List[str]:
    """Validate an AI-compute workload packet; returns list of error strings (empty = valid)."""
    errs: List[str] = []

    if not isinstance(packet, dict):
        return ["packet must be a JSON object"]
    if packet.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}")
    if "label" not in packet or not isinstance(packet["label"], str):
        errs.append("missing 'label' (string)")

    # ------------------------------------------------------------ site
    site = packet.get("site")
    if not isinstance(site, dict):
        return errs + ["missing/blank 'site' object"]
    if not site.get("id"):
        errs.append("site: missing 'id'")
    if "currency" not in site or not isinstance(site["currency"], str):
        errs.append("site: missing 'currency' (e.g. AUD)")
    if "time_zone" not in site or not isinstance(site["time_zone"], str):
        errs.append("site: missing 'time_zone'")
    im = _num(site.get("interval_minutes"), 1)
    if im is None or 60 % im != 0:
        errs.append("site: interval_minutes must be a positive divisor of 60")

    base = _num(site.get("it_base_load_kw"), 0.0)
    if base is None:
        errs.append("site: it_base_load_kw must be a number >= 0 (kW)")
    incr = _num(site.get("incremental_kw_per_gpu"), 0.0)
    if incr is None:
        errs.append("site: incremental_kw_per_gpu must be a number > 0 (kW per active-GPU)")
    pue = _num(site.get("pue"), 1.0)
    if pue is None:
        errs.append("site: pue must be a number >= 1 (dimensionless)")
    cap = site.get("grid_power_cap_kw")
    if cap is not None:
        c = _num(cap, 0.0)
        if c is None:
            errs.append("site: grid_power_cap_kw must be a number > 0 (kW) or null")
        cap = c
    flex = _num(site.get("flex_compute_capacity_gpu"), 0.0)
    if flex is None:
        errs.append("site: flex_compute_capacity_gpu must be a number > 0 (active-GPU equivalents)")
    budget = site.get("budget")
    if budget is not None:
        b = _num(budget, 0.0)
        if b is None:
            errs.append("site: budget must be a number > 0 (currency) or null")
        budget = b

    # grid cap vs base load sanity (base alone may already exceed cap)
    if cap is not None and pue is not None and base is not None:
        if pue * base > cap + TOL_GPU:
            errs.append(
                "site: grid cap too low for base load alone: "
                f"PUE*base={pue * base:.4f} kW > cap={cap:.4f} kW"
            )

    # ------------------------------------------------------------ tariff
    tar = packet.get("tariff")
    if not isinstance(tar, dict):
        errs.append("missing/blank 'tariff' object")
        return errs
    kind = tar.get("kind")
    if kind not in ("flat", "dynamic"):
        errs.append("tariff: kind must be 'flat' or 'dynamic'")
    eff = tar.get("effective_date")
    if eff is not None:
        try:
            date.fromisoformat(eff)
        except (TypeError, ValueError):
            errs.append(f"tariff: effective_date not YYYY-MM-DD: {eff!r}")
    exp = tar.get("expiry_date")
    if exp is not None:
        try:
            date.fromisoformat(exp)
        except (TypeError, ValueError):
            errs.append(f"tariff: expiry_date not YYYY-MM-DD: {exp!r}")
    if eff is not None and exp is not None and isinstance(eff, str) and isinstance(exp, str):
        try:
            if date.fromisoformat(exp) < date.fromisoformat(eff):
                errs.append("tariff: expiry_date earlier than effective_date")
        except (TypeError, ValueError):
            pass

    if kind == "flat":
        rate = _num(tar.get("rate_aud_per_kwh"), 0.0)
        if rate is None:
            errs.append("tariff: flat kind requires rate_aud_per_kwh > 0")
        if tar.get("table") is not None:
            errs.append("tariff: flat kind must not carry a 'table'")
    elif kind == "dynamic":
        table = tar.get("table")
        if not isinstance(table, list) or not table:
            errs.append("tariff: dynamic kind requires non-empty 'table'")
        else:
            # window-relative coverage when the window parsed OK
            try:
                w_s = parse_iso(packet["window"]["start_iso"])
                w_e = parse_iso(packet["window"]["end_iso"])
                errs += validate_tariff_table(table, w_s, w_e)
            except (KeyError, ContractError):
                errs += validate_tariff_table(table)
        if tar.get("rate_aud_per_kwh") is not None:
            errs.append("tariff: dynamic kind must not carry flat 'rate_aud_per_kwh'")

    # ------------------------------------------------------------ window
    w = packet.get("window")
    if not isinstance(w, dict):
        errs.append("missing/blank 'window' object")
        return errs
    try:
        w_start = parse_iso(w["start_iso"])
        w_end = parse_iso(w["end_iso"])
    except (KeyError, ContractError) as e:
        errs.append(f"window: invalid start_iso/end_iso: {e}")
        return errs
    if w_start >= w_end:
        errs.append("window: start_iso must be < end_iso")
    # stale dates: the scheduler supports historical replay, so a past window is only
    # flagged when the packet explicitly asks (meta.check_stale=true) — edge-case test.
    if packet.get("meta", {}).get("check_stale") and w_end < datetime.now(w_end.tzinfo):
        errs.append("window: end_iso is in the past (stale schedule window)")

    # ------------------------------------------------------------ jobs
    jobs = packet.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        errs.append("missing/blank 'jobs' list (must have >= 1 job)")
        return errs
    seen_ids = set()
    for j in jobs:
        if not isinstance(j, dict):
            errs.append("jobs: each job must be an object")
            continue
        jid = j.get("id")
        if not jid:
            errs.append("job: missing 'id'")
        elif jid in seen_ids:
            errs.append(f"job: duplicate id {jid!r}")
        else:
            seen_ids.add(jid)
        req = _num(j.get("required_gpu_hours"), 0.0)
        if req is None:
            errs.append(f"job {jid!r}: required_gpu_hours must be a number > 0 (GPU-hours)")
        mp = _num(j.get("max_parallel_gpu"), 0.0)
        if mp is None:
            errs.append(f"job {jid!r}: max_parallel_gpu must be a number > 0 (active-GPU)")
        try:
            rel = parse_iso(j["release_iso"])
            dl = parse_iso(j["deadline_iso"])
        except (KeyError, ContractError) as e:
            errs.append(f"job {jid!r}: invalid release_iso/deadline_iso: {e}")
            continue
        if rel >= dl:
            errs.append(f"job {jid!r}: release_iso must be < deadline_iso")
        if rel < w_start - _TOL_DT or dl > w_end + _TOL_DT:
            errs.append(f"job {jid!r}: [release,deadline) must lie inside the schedule window")
        if req is not None and mp is not None and rel < dl:
            win_h = (dl - rel).total_seconds() / 3600.0
            max_work = mp * win_h
            if req > max_work + TOL_GPU:
                errs.append(
                    f"job {jid!r}: infeasible window — required_gpu_hours={req:.4f} > "
                    f"max_parallel_gpu*window_hours={max_work:.4f}"
                )

    # aggregate feasibility (necessary condition): total work <= effective capacity * hours
    if isinstance(packet.get("window"), dict) and jobs and not errs:
        try:
            win_h = (w_end - w_start).total_seconds() / 3600.0
            total_req = sum(float(j["required_gpu_hours"]) for j in jobs)
            cap_eff = flex if flex is not None else 0.0
            if cap is not None and pue is not None and base is not None and incr is not None:
                alloc_ub = (cap / pue - base) / incr if incr else None
                if alloc_ub is not None:
                    cap_eff = min(cap_eff, alloc_ub)
            if cap_eff is not None and total_req > cap_eff * win_h + TOL_GPU:
                errs.append(
                    "aggregate infeasible: total required GPU-hours "
                    f"{total_req:.4f} > effective capacity {cap_eff:.4f} GPU x {win_h:.4f} h"
                )
        except (KeyError, TypeError, ValueError):
            pass  # already flagged above

    return errs


# ---------------------------------------------------------------- derived quantities (shared)

def compute_derived(packet: Dict[str, Any]) -> Dict[str, Any]:
    """Precompute normalized numeric fields for the solvers (after validation passed).

    Returns site/tariff/window/jobs numbers with units explicit in keys.
    """
    site, tar, w = packet["site"], packet["tariff"], packet["window"]
    w_start = parse_iso(w["start_iso"])
    w_end = parse_iso(w["end_iso"])
    interval_minutes = int(site["interval_minutes"])
    intervals = build_intervals(w_start, w_end, interval_minutes)

    if tar["kind"] == "flat":
        flat_rate = float(tar["rate_aud_per_kwh"])

        def rate_of(_dt):
            return flat_rate

        tariff_kind = "flat"
    else:
        rows = sorted(tar["table"], key=lambda r: (int(r["start"][:2]), int(r["start"][3:5])))
        wall = [
            (int(r["start"][:2]) * 60 + int(r["start"][3:5]),
             int(r["end"][:2]) * 60 + int(r["end"][3:5]),
             float(r["rate_aud_per_kwh"]))
            for r in rows
        ]

        def rate_of(dt):
            t = dt.hour * 60 + dt.minute
            for s, e, rate in wall:
                if s <= t < e:
                    return rate
            raise ContractError(f"no tariff row covers local time {dt.strftime('%H:%M')}")

        tariff_kind = "dynamic"

    rates = [rate_of(s) for s, _, _ in intervals]

    jobs = []
    for j in packet["jobs"]:
        rel = parse_iso(j["release_iso"])
        dl = parse_iso(j["deadline_iso"])
        jobs.append({
            "id": j["id"],
            "release": rel,
            "deadline": dl,
            "required_gpu_hours": float(j["required_gpu_hours"]),
            "max_parallel_gpu": float(j["max_parallel_gpu"]),
        })

    return {
        "site": {
            "currency": site["currency"],
            "interval_minutes": interval_minutes,
            "it_base_load_kw": float(site["it_base_load_kw"]),
            "incremental_kw_per_gpu": float(site["incremental_kw_per_gpu"]),
            "pue": float(site["pue"]),
            "grid_power_cap_kw": (None if site.get("grid_power_cap_kw") is None
                                  else float(site["grid_power_cap_kw"])),
            "flex_compute_capacity_gpu": float(site["flex_compute_capacity_gpu"]),
            "budget": (None if site.get("budget") is None else float(site["budget"])),
        },
        "tariff_kind": tariff_kind,
        "intervals": intervals,   # list of (start_dt, end_dt, hours)
        "rates_currency_per_kwh": rates,
        "jobs": jobs,
    }
