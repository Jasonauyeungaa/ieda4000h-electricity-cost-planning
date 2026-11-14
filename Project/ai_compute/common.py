"""ai_compute shared I/O + interval utilities (AI-company workload module).

SCHEMA_VERSION "1.0" (AI-compute scope). Units are explicit everywhere:
GPU-hours, kW, kWh, AUD/kWh (or declared site currency), hours.

Attribution: interval slicing / ISO parsing / JSON I/O / tolerance conventions
are adapted from the EV module `h/scheduler/common.py` (same project, EV scope).
The AI-compute module re-implements them locally (never imports h/) with
semantics adjusted for GPU-workload scheduling. See MIGRATION_PLAN.md reuse table.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

SCHEMA_VERSION = "1.0"
TOL_ENERGY = 1e-6          # kWh / kW
TOL_MONEY = 1e-6           # AUD (or declared currency)
TOL_GPU = 1e-6             # GPU / GPU-hours
PDIG = 12                  # precision for printing / rounding


class ContractError(ValueError):
    """Raised when an input violates the AI-compute I/O contract."""


# ---------------------------------------------------------------- time helpers

def parse_iso(s: str) -> datetime:
    """Parse ISO 8601 with explicit offset; naive timestamps are rejected.

    Same rule as h/scheduler/common.py::parse_iso (attribution above).
    """
    if not isinstance(s, str) or "T" not in s:
        raise ContractError(f"invalid ISO time: {s!r}")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ContractError(f"naive timestamp rejected (no UTC offset): {s!r}")
    if dt.utcoffset().total_seconds() % 60 != 0:
        raise ContractError(f"offset must be whole minutes: {s!r}")
    return dt


def to_iso(dt: datetime) -> str:
    """Render a datetime as ISO 8601 with the SAME offset it carries."""
    off = dt.utcoffset()
    if off is None:
        raise ContractError("internal: naive datetime")
    total_min = int(off.total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    ah, am = divmod(abs(total_min), 60)
    return dt.strftime(f"%Y-%m-%dT%H:%M:%S{sign}{ah:02d}:{am:02d}")


# ---------------------------------------------------------------- interval math

def build_intervals(start: datetime, end: datetime,
                    interval_minutes: int) -> List[Tuple[datetime, datetime, float]]:
    """Slice [start, end) into intervals; returns (start, end, hours).

    interval_minutes must divide 60 (grid-aligned, same rule as EV build_intervals).
    """
    if start >= end:
        raise ContractError("window start_iso must be < end_iso")
    if 60 % interval_minutes != 0:
        raise ContractError(f"interval_minutes must divide 60: {interval_minutes}")
    for label, dt in (("start", start), ("end", end)):
        if dt.minute % interval_minutes != 0 or dt.second != 0 or dt.microsecond != 0:
            raise ContractError(f"{label} must fall on the {interval_minutes}-min grid: {to_iso(dt)}")
    out: List[Tuple[datetime, datetime, float]] = []
    t = start
    while t < end:
        nxt = min(t + timedelta(minutes=interval_minutes), end)
        hours = (nxt - t).total_seconds() / 3600.0
        out.append((t, nxt, hours))
        t = nxt
    return out


def wall_clock_minutes(dt: datetime) -> int:
    """Local wall-clock minutes since midnight (UTC offset preserved)."""
    return dt.hour * 60 + dt.minute


# ---------------------------------------------------------------- JSON I/O

def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def r12(x: float) -> float:
    """Round to 12 decimals for stable reporting."""
    return round(float(x), PDIG)
