# ---------------------------------------------------------------------------
# SUPERSEDED — DO NOT USE FOR NEW WORK.
#
# This module is a dead second forecast contract. Nothing in the project imports it.
# The live pipeline validates every export against `electricity.forecast.v2` in
# `study/core.py::validate`, which is the single authoritative schema.
#
# It is retained only so older notes keep a working reference. Do not extend it, do not
# route new exports through it, and do not treat the field list below as a contract.
# ---------------------------------------------------------------------------
"""M8 — machine-readable, versioned forecast-output interface.

Schema and writer for forecast records. Records are ALWAYS written from real executed runs
(predictions.csv + config.json) — never fabricated.

Fields (see schema.json / schema.md for the full contract):
  forecast_id, model_id, model_version, region, issue_time, target_start,
  target_end, forecast_price_AUD_per_MWh, demand_forecast_MW (null: demand is an
  input feature, not a forecast output), uncertainty_ref (null: no validated
  uncertainty/scenario model), event_feature_version ('none' for base price models),
  data_cutoff, actual_price_AUD_per_MWh (null until observed),
  verification_status, run_id, schema_version.

Conventions (from the M3 run spec):
  - origin = daily 00:00 NEM UTC+10; horizon = 48 half-hour slots of the same day
  - issue_time = origin 00:00 +10:00 (nominal day-ahead)
  - data_cutoff = last completed interval START before origin = origin - 30 min
  - target_start = interval START label; target_end = target_start + 30 min
  - price unit AUD/MWh; timezone NEM UTC+10 (naive labels + +10:00 suffix)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = "1.0"
REGION = "NSW1"
SLOT_MINUTES = 30


# --------------------------------------------------------------------------
# Schema (JSON Schema draft-07) — also written to disk as schema.json
# --------------------------------------------------------------------------
def schema() -> dict:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "GridShield NSW1 forecast output record",
        "description": ("One 30-min forecast target for NSW1 RRP (AUD/MWh). "
                        "Machine-readable NSW1 forecast record."),
        "type": "object",
        "required": [
            "forecast_id", "model_id", "model_version", "region", "issue_time",
            "target_start", "target_end", "forecast_price_AUD_per_MWh",
            "demand_forecast_MW", "uncertainty_ref", "event_feature_version",
            "data_cutoff", "actual_price_AUD_per_MWh", "verification_status",
            "run_id", "schema_version",
        ],
        "properties": {
            "forecast_id": {"type": "string",
                            "description": "sha256(model_version:model_id:target_start)[:16]"},
            "model_id": {"type": "string",
                         "description": "e.g. cnn_FG2 (kind_featuregroup)"},
            "model_version": {"type": "string",
                              "description": "training run id, e.g. m3_20261110T081713Z"},
            "region": {"const": "NSW1"},
            "issue_time": {"type": "string", "format": "date-time",
                           "description": "nominal day-ahead issue time, NEM UTC+10"},
            "target_start": {"type": "string", "format": "date-time",
                             "description": "target interval START, NEM UTC+10"},
            "target_end": {"type": "string", "format": "date-time",
                           "description": "target interval END = start + 30 min"},
            "forecast_price_AUD_per_MWh": {"type": "number"},
            "demand_forecast_MW": {"type": ["number", "null"],
                                   "description": "null: demand is an input feature, not a forecast output"},
            "uncertainty_ref": {"type": ["string", "null"],
                                "description": "null: no validated uncertainty/scenario model"},
            "event_feature_version": {"type": "string",
                                      "description": "'none' for base price models (no event features)"},
            "data_cutoff": {"type": "string", "format": "date-time",
                            "description": "latest observation used = origin - 30 min"},
            "actual_price_AUD_per_MWh": {"type": ["number", "null"],
                                         "description": "finalized actual, null until observed"},
            "verification_status": {"type": "string",
                                    "enum": ["issued", "actual_observed"],
                                    "description": "issued=no actual yet; actual_observed=error computed"},
            "run_id": {"type": "string"},
            "schema_version": {"const": SCHEMA_VERSION},
        },
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------
# Record builder
# --------------------------------------------------------------------------
def forecast_id(model_version: str, model_id: str, target_start: str) -> str:
    key = f"{model_version}:{model_id}:{target_start}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def build_record(model_id: str, model_version: str, target_start: str,
                 yhat: float, y: float | None, run_id: str,
                 demand_mw: float | None = None,
                 uncertainty_ref: str | None = None,
                 event_feature_version: str = "none") -> dict:
    """One interface record. yhat/y in AUD/MWh; target_start is a str like
    '2025-01-01 00:00:00+10:00' (interval START, NEM UTC+10)."""
    ts = pd.Timestamp(target_start)
    origin = ts.normalize()                       # daily origin 00:00 same day
    data_cutoff = origin - pd.Timedelta(minutes=SLOT_MINUTES)
    rec = {
        "forecast_id": forecast_id(model_version, model_id, target_start),
        "model_id": model_id,
        "model_version": model_version,
        "region": REGION,
        "issue_time": origin.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
        "target_start": ts.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
        "target_end": (ts + pd.Timedelta(minutes=SLOT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S+10:00"),
        "forecast_price_AUD_per_MWh": round(float(yhat), 4),
        "demand_forecast_MW": demand_mw,
        "uncertainty_ref": uncertainty_ref,
        "event_feature_version": event_feature_version,
        "data_cutoff": data_cutoff.strftime("%Y-%m-%dT%H:%M:%S+10:00"),
        "actual_price_AUD_per_MWh": (round(float(y), 4) if y is not None and pd.notna(y) else None),
        "verification_status": "actual_observed" if (y is not None and pd.notna(y)) else "issued",
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
    }
    return rec


# --------------------------------------------------------------------------
# Writer from a REAL m3 run directory
# --------------------------------------------------------------------------
def write_from_m3_run(run_dir: Path, out_dir: Path, methods=None):
    """Read <run_dir>/predictions.csv + config.json and write JSONL records.

    out_dir receives: schema.json, schema.md, forecast_records_<method>.jsonl,
    preview.jsonl and README.md. Returns summary dict.
    """
    run_dir = Path(run_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pred = pd.read_csv(run_dir / "predictions.csv")
    config = json.loads((run_dir / "config.json").read_text())
    version = config["version"]

    if methods is None:
        methods = sorted(pred["method"].unique().tolist())

    counts = {}
    for method in methods:
        sub = pred[pred["method"] == method]
        rows = [build_record(method, version, t, yhat, y, run_id=version)
                for t, yhat, y in zip(sub["target"], sub["yhat"], sub["y"])]
        path = out_dir / f"forecast_records_{method}.jsonl"
        with path.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        counts[method] = len(rows)

    # small human preview: first 3 targets of the first method
    preview = []
    for method in methods:
        sub = pred[pred["method"] == method]
        for t, yhat, y in zip(sub["target"][:3], sub["yhat"][:3], sub["y"][:3]):
            preview.append(build_record(method, version, t, yhat, y, run_id=version))
    (out_dir / "preview.jsonl").write_text("\n".join(json.dumps(r) for r in preview) + "\n")

    # schema artifacts
    (out_dir / "schema.json").write_text(json.dumps(schema(), indent=2))
    (out_dir / "schema.md").write_text(schema_markdown())

    readme = _readme_text(version, counts)
    (out_dir / "README.md").write_text(readme)

    return {"run": version, "records": counts, "out_dir": str(out_dir),
            "methods": methods}


def schema_markdown() -> str:
    lines = ["# GridShield NSW1 forecast-output schema (v1.0)", "",
             "One JSON object per 30-min target interval; JSONL files, one record "
             "per line. All timestamps are NEM UTC+10 (naive market labels with "
             "+10:00 suffix). Price unit: AUD/MWh.", "",
             "| field | type | meaning |",
             "|---|---|---|"]
    props = schema()["properties"]
    for name, p in props.items():
        desc = p.get("description", "")
        typ = p.get("type", p.get("const", "?"))
        lines.append(f"| {name} | {typ} | {desc} |")
    lines += ["", "`verification_status`: `issued` = no actual yet; "
              "`actual_observed` = finalized actual recorded and error computable.",
              "", "`demand_forecast_MW` and `uncertainty_ref` are null in the base "
              "price interface (demand is an input feature, not a forecast output; no "
              "validated uncertainty/scenario model exists)."]
    return "\n".join(lines)


def _readme_text(version: str, counts: dict) -> str:
    total = sum(counts.values())
    return f"""# M8 forecast-output interface — sample files (real runs)

- **Schema version**: {SCHEMA_VERSION}
- **Source run**: {version}
- **Records written**: {total} across {len(counts)} methods:
{chr(10).join(f'  - {k}: {v}' for k, v in counts.items())}

Files:
- `schema.json` / `schema.md` — the machine-readable contract.
- `forecast_records_<method>.jsonl` — full held-out test predictions
  (2025-01-01 … 2026-10-31, 48 slots/day) written as interface records.
- `preview.jsonl` — first 3 targets per method for quick inspection.

All records are generated from the executed run's saved predictions.csv and
config.json by `02_Project/learning/h_interface.py::write_from_m3_run()`.
No fabricated samples.

## Reading exported forecasts

1. Pick the method (e.g. `cnn_FG2`) and read its JSONL (one record per line).
2. Filter by `target_start` for the horizon needed (48 consecutive slots/day).
3. Use `forecast_price_AUD_per_MWh` as the forecast price input to the
   scheduling / budgeting model. `actual_price_AUD_per_MWh` becomes available
   once published (`verification_status` flips to `actual_observed`) for
   frozen-plan scoring.
4. Respect `data_cutoff` / `issue_time` — inputs end at or before the cutoff;
   do not mix later information into an earlier issue.
5. `model_version` + `forecast_id` identify the exact model/target pair;
   `event_feature_version` records whether event features were active ('none'
   for these base price models).
"""
