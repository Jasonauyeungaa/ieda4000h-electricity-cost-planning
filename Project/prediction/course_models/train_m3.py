"""M3 reproducible forecasting engine — train + evaluate (run spec: control_docs/M3_RUN_SPEC.md).

Daily-origin next-24h (48 half-hour) NSW1 RRP forecasting:
  baselines (prev-day, prev-week) + FNN (L4) / CNN1D (L5) / LSTM (L6)
  feature groups FG1 (price) / FG2 (+demand) / FG3 (+weekly D-7)
  chronological splits: train 2016-11-08..2023-12-31, val 2024, test 2025-01-01..2026-10-31
  L1 (MAE) loss, Adam mini-batch, early stopping on validation MAE, weight decay,
  linear 48-output regression head (negative prices possible).
"""
from __future__ import annotations

import argparse, copy, datetime as dt, hashlib, json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from .history import DATA, load
from .models import build_model

REGISTRY = DATA / "outputs" / "m3"
SLOTS = 48
SEQ = 96  # prior 48h window (2 days x 48 slots)
CAL_DIM = 13  # 7 dow + 2 annual + 2 origin-hour + 1 regime + 1 weekend


def calendar_features(origin: pd.Timestamp) -> np.ndarray:
    phase = 2 * np.pi * ((origin.day_of_year - 1) + origin.hour / 24) / 365.2425
    hour = 2 * np.pi * (origin.hour + origin.minute / 60) / 24
    dow = np.zeros(7, dtype=np.float32)
    dow[origin.day_of_week] = 1.0
    return np.concatenate([
        dow,
        [np.sin(phase), np.cos(phase), np.sin(hour), np.cos(hour)],
        [float(origin >= pd.Timestamp("2021-10-01"))],  # 5-min settlement regime
        [float(origin.day_of_week >= 5)],  # weekend
    ]).astype(np.float32)


def sample(frame: pd.DataFrame, origin: pd.Timestamp, targets: bool = True):
    """Return (a, cal, w, y): a=(96,2) price+demand prior window, cal=(13,),
    w=(48,) weekly profile, y=(48,) targets. All observations end at/before origin."""
    a = frame.reindex(pd.date_range(origin - pd.Timedelta(hours=48), periods=SEQ,
                                    freq="30min"))[["price", "demand"]].to_numpy(np.float32)
    w = frame.reindex(pd.date_range(origin - pd.Timedelta(days=7), periods=SLOTS,
                                    freq="30min")).price.to_numpy(np.float32)
    y = (frame.reindex(pd.date_range(origin, periods=SLOTS, freq="30min")).price
         .to_numpy(np.float32) if targets else None)
    if not np.isfinite(a).all() or not np.isfinite(w).all() or (targets and not np.isfinite(y).all()):
        return None
    return a, calendar_features(origin), w, y


def metrics(pred, actual):
    e = np.asarray(pred) - np.asarray(actual)
    ss_res = float(np.sum(e * e))
    ss_tot = float(np.sum((np.asarray(actual) - np.mean(actual)) ** 2))
    return {
        "mae": float(np.mean(np.abs(e))),
        "mse": float(np.mean(e * e)),
        "rmse": float(np.sqrt(np.mean(e * e))),
        "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan"),
        "bias": float(e.mean()),
        "max_abs_err": float(np.max(np.abs(e))) if e.size else 0.0,
        "n": int(e.size),
    }


def tensors(x, c, w, fg, sc):
    channels = 1 if fg == "FG1" else 2
    xx = ((x[:, :, :channels] - np.array(sc["x_mean"])) / np.array(sc["x_std"])).astype(np.float32)
    extra = (np.concatenate([c, (w - sc["y_mean"]) / sc["y_std"]], axis=1)
             if fg == "FG3" else c)
    return torch.tensor(xx, dtype=torch.float32), torch.tensor(extra, dtype=torch.float32)


def breakdowns(pred, actual, slot_starts, spike_p99):
    """Pooled metrics overall + by month, season, negative-price and spike periods."""
    pred = np.asarray(pred).ravel()
    actual = np.asarray(actual).ravel()
    out = {"overall": metrics(pred, actual)}
    ts = pd.DatetimeIndex(slot_starts)
    pdf = pd.DataFrame({"y": np.asarray(actual), "yhat": np.asarray(pred)})
    pdf["month"] = ts.month
    pdf["season"] = ts.month.map({12: "Summer", 1: "Summer", 2: "Summer",
                                  3: "Autumn", 4: "Autumn", 5: "Autumn",
                                  6: "Winter", 7: "Winter", 8: "Winter",
                                  9: "Spring", 10: "Spring", 11: "Spring"})
    pdf["neg"] = pdf.y < 0
    pdf["spike_p99"] = pdf.y >= spike_p99
    pdf["spike_500"] = pdf.y >= 500.0
    out["by_month"] = {int(k): metrics(v.yhat, v.y) for k, v in pdf.groupby("month")}
    out["by_season"] = {k: metrics(v.yhat, v.y) for k, v in pdf.groupby("season")}
    out["negative_price_periods"] = (metrics(pdf.yhat[pdf.neg], pdf.y[pdf.neg])
                                     if pdf.neg.any() else {"n": 0})
    out["spike_periods_p99"] = (metrics(pdf.yhat[pdf.spike_p99], pdf.y[pdf.spike_p99])
                                if pdf.spike_p99.any() else {"n": 0})
    out["spike_periods_ge500"] = (metrics(pdf.yhat[pdf.spike_500], pdf.y[pdf.spike_500])
                                  if pdf.spike_500.any() else {"n": 0})
    return out


def model_forward(net, kind, Xb, Eb):
    """Forward pass per model kind: FNN=flat+extras, CNN=channels-first, LSTM=(96,C)."""
    if kind == "fnn":
        return net(torch.cat([Xb.flatten(1), Eb], dim=1))
    if kind == "cnn":
        return net(Xb.transpose(1, 2), Eb)
    return net(Xb, Eb)  # lstm: (B, 96, C) batch_first


def train_model(kind, fg, X, E, Y, y, train_idx, val_idx, test_idx, sc,
                epochs, seed, verbose=True):
    """Train one model; return (net, state_dict, test_pred, best_val_mae, curve)."""
    torch.manual_seed(seed)
    net = build_model(kind, 1 if fg == "FG1" else 2, E.shape[1], seed=seed)
    optimizer = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    best, stale, state, curve = float("inf"), 0, None, []

    def predict(idx):
        with torch.no_grad():
            chunks = np.array_split(idx, max(1, len(idx) // 256))
            return np.concatenate([model_forward(net, kind, X[i], E[i]).numpy()
                                   for i in chunks])

    for epoch in range(epochs):
        net.train()
        order = torch.tensor(train_idx)[torch.randperm(len(train_idx))]
        for idx in order.split(256):
            optimizer.zero_grad()
            out = model_forward(net, kind, X[idx], E[idx])
            loss = torch.nn.functional.l1_loss(out, Y[idx])
            loss.backward()
            optimizer.step()
        net.eval()
        v = predict(val_idx)
        score = metrics(v * sc["y_std"] + sc["y_mean"], y[val_idx])["mae"]
        curve.append({"epoch": epoch + 1, "validation_mae": score})
        if verbose:
            print(f"  {kind} {fg} epoch {epoch+1} val MAE {score:.3f}", flush=True)
        if score < best - 1e-5:
            best, state, stale = score, copy.deepcopy(net.state_dict()), 0
        else:
            stale += 1
            if stale >= 6:
                break
    net.load_state_dict(state)
    net.eval()
    p = predict(test_idx)
    return net, state, p * sc["y_std"] + sc["y_mean"], best, curve


def flat_slot_starts(origins):
    """Target slot START labels for a set of daily origins (48 per origin)."""
    return pd.DatetimeIndex([o + pd.Timedelta(minutes=30 * s)
                             for o in origins for s in range(SLOTS)])


def plot_curves(directory: Path):
    """Write loss-curve PNGs from saved curves/*.csv (best-effort; never fatal)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    files = sorted((directory / "curves").glob("*.csv"))
    if not files:
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    for f in files:
        df = pd.read_csv(f)
        ax.plot(df["epoch"], df["validation_mae"], label=f.stem, marker=".")
    ax.set_xlabel("epoch")
    ax.set_ylabel("validation MAE (AUD/MWh)")
    ax.set_title(f"Loss curves — {directory.name}")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(directory / "loss_curves.png", dpi=110)
    plt.close(fig)


def run_all(fg, kinds, frame, origins, x, c, w, y, masks, spike_p99,
            epochs, seed, verbose=True):
    """Train every (kind, fg) on the same origins; returns results dict."""
    ti = np.flatnonzero(masks["train"])
    vi = np.flatnonzero(masks["validation"])
    te = np.flatnonzero(masks["test"])
    y_te = y[te]
    slot_starts = flat_slot_starts(origins[te])
    prev_day = np.stack([frame.reindex(pd.date_range(o - pd.Timedelta(days=1),
                                                     periods=SLOTS, freq="30min")).price.to_numpy()
                         for o in origins[te]])
    results = {
        "baselines": {"prev-day": metrics(prev_day, y_te),
                      "prev-week": metrics(w[te], y_te)},
        "models": {}, "spike_p99": spike_p99,
        "test_slot_starts": [str(t) + "+10:00" for t in slot_starts],
        "test_y": y_te.tolist(),
    }
    for fg_name in fg:
        channels = 1 if fg_name == "FG1" else 2
        sc = {
            "x_mean": x[ti, :, :channels].mean(axis=(0, 1)).tolist(),
            "x_std": np.maximum(x[ti, :, :channels].std(axis=(0, 1)), 1e-6).tolist(),
            "y_mean": float(y[ti].mean()),
            "y_std": float(max(y[ti].std(), 1e-6)),
        }
        X, E = tensors(x, c, w, fg_name, sc)
        Y = torch.tensor((y - sc["y_mean"]) / sc["y_std"], dtype=torch.float32)
        for kind in kinds:
            key = f"{kind}_{fg_name}"
            net, state, pred, best, curve = train_model(
                kind, fg_name, X, E, Y, y, ti, vi, te, sc, epochs, seed, verbose)
            results["models"][key] = {
                "validation_mae": best,
                "curve": curve,
                "test": breakdowns(pred, y_te, slot_starts, spike_p99),
                "pred": pred.tolist(),
                "scalers": sc,
                "state": state,
                "arch": {"kind": kind, "fg": fg_name, "channels": channels,
                         "extras": int(E.shape[1]), "seed": seed},
            }
    return results


def main():
    ap = argparse.ArgumentParser(description="M3 forecasting engine (train + eval)")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--kinds", default="fnn,cnn,lstm")
    ap.add_argument("--fg", default="FG1,FG2,FG3")
    ap.add_argument("--train-start", default="2016-11-08")
    ap.add_argument("--train-end", default="2023-12-31")
    ap.add_argument("--validation-end", default="2024-12-31")
    ap.add_argument("--test-end", default="2026-10-31")
    ap.add_argument("--recent-only", action="store_true")
    ap.add_argument("--note", default="")
    ap.add_argument("--include-market", action="store_true",
                    help="Include complete half-hours in the local market database")
    args = ap.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    fg = [f.strip() for f in args.fg.split(",") if f.strip()]
    torch.set_num_threads(2)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    frame = load()
    if args.include_market:
        from forecast_engine import market_frame
        frame = market_frame()
        frame = frame.loc[frame.index + pd.Timedelta(minutes=30) <= pd.Timestamp.now(tz="Etc/GMT-10").tz_localize(None)]
    start = frame.index.min() + pd.Timedelta(days=7)  # D-7 must exist
    train_start = pd.Timestamp(args.train_start if args.train_start != "auto" else start)
    train_end = pd.Timestamp(args.train_end)
    val_end = pd.Timestamp(args.validation_end)
    test_end = pd.Timestamp(args.test_end)
    if args.recent_only:
        train_start = pd.Timestamp("2022-01-01")
        train_end = pd.Timestamp("2023-12-31")
    if not (train_start <= train_end < val_end < test_end):
        raise ValueError("Cutoffs must be chronological")

    records = []
    for origin in pd.date_range(start, frame.index.max() - pd.Timedelta(hours=23.5), freq="D"):
        s = sample(frame, origin)
        if s is not None:
            records.append((origin, *s))
    origins = pd.DatetimeIndex([r[0] for r in records])
    x = np.stack([r[1] for r in records])
    c = np.stack([r[2] for r in records])
    w = np.stack([r[3] for r in records])
    y = np.stack([r[4] for r in records])
    ends = origins + pd.Timedelta(days=1)  # target window END labels
    masks = {
        "train": np.asarray((origins >= train_start) & (ends <= train_end + pd.Timedelta(days=1))),
        "validation": np.asarray((origins > train_end) & (ends <= val_end + pd.Timedelta(days=1))),
        "test": np.asarray((origins > val_end) & (ends <= test_end + pd.Timedelta(days=1))),
    }
    if any(int(m.sum()) < 10 for m in masks.values()):
        raise ValueError("Insufficient chronological train/validation/test origins")
    spike_p99 = float(np.percentile(y[masks["train"]], 99))

    version = dt.datetime.now(dt.timezone.utc).strftime("m3_%Y%m%dT%H%M%S%fZ")
    directory = REGISTRY / version
    (directory / "weights").mkdir(parents=True, exist_ok=True)
    (directory / "curves").mkdir(exist_ok=True)

    manifest = {
        "version": version,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": args.note,
        "seed": args.seed,
        "epochs_max": args.epochs,
        "loss": "L1Loss (MAE)",
        "optimizer": "Adam lr=1e-3 weight_decay=1e-4",
        "batch": 256,
        "early_stopping": "patience 6 on validation MAE",
        "horizon": "48 half-hours / next 24h",
        "origin_frequency": "1 per day (non-overlapping target windows)",
        "recent_only": args.recent_only,
        "includes_market_database": args.include_market,
        "feature_definition": ("Prior 96 price/demand slots, optional D-7 profile, "
                               "origin weekday/annual/hour/regime/weekend calendar. "
                               "All observations end at/before origin."),
        "splits": {
            "train": {"first_origin": str(origins[masks["train"]][0]),
                      "last_origin": str(origins[masks["train"]][-1]),
                      "n": int(masks["train"].sum())},
            "validation": {"first_origin": str(origins[masks["validation"]][0]),
                           "last_origin": str(origins[masks["validation"]][-1]),
                           "n": int(masks["validation"].sum())},
            "test": {"first_origin": str(origins[masks["test"]][0]),
                     "last_origin": str(origins[masks["test"]][-1]),
                     "n": int(masks["test"].sum())},
        },
        "data": {"rows": int(len(frame)), "first": str(frame.index.min()),
                 "last_end": str(frame.index.max() + pd.Timedelta(minutes=30)),
                 "regime": "30min pre-2021-10-01, 5min from 2021-10-01",
                 "price_demand_frame_sha256": hashlib.sha256(
                     pd.util.hash_pandas_object(frame[["price", "demand"]], index=True).to_numpy().tobytes()
                 ).hexdigest()},
        "spike_threshold_p99_train": spike_p99,
    }
    (directory / "config.json").write_text(json.dumps(manifest, indent=2))

    results = run_all(fg, kinds, frame, origins, x, c, w, y, masks, spike_p99,
                      args.epochs, args.seed)
    results["manifest"] = manifest

    # ---- persist artifacts ----
    pred_rows = []
    for key, m in results["models"].items():
        wgt = directory / "weights" / f"{key}_seed{args.seed}.pt"
        torch.save(m["state"], wgt)
        m["weight_sha256"] = hashlib.sha256(wgt.read_bytes()).hexdigest()
        pd.DataFrame(m["curve"]).to_csv(directory / "curves" / f"{key}.csv", index=False)
        starts = results["test_slot_starts"]
        yflat = np.asarray(results["test_y"]).ravel()
        pflat = np.asarray(m["pred"]).ravel()
        for i in range(len(starts)):
            pred_rows.append({"method": key, "target": starts[i],
                              "y": float(yflat[i]), "yhat": float(pflat[i]),
                              "err": float(pflat[i] - yflat[i])})
    pd.DataFrame(pred_rows).to_csv(directory / "predictions.csv", index=False)

    clean = {}
    for key, m in results["models"].items():
        clean[key] = {k2: v for k2, v in m.items() if k2 != "state"}
    clean["baselines"] = results["baselines"]
    clean["spike_p99"] = results["spike_p99"]
    clean["manifest"] = manifest
    (directory / "metrics.json").write_text(json.dumps(clean, indent=2))

    # markdown metrics table
    lines = [f"# M3 metrics — {version}", "",
             "| method | MAE | MSE | RMSE | R² | bias | max\\|err\\| | n |", "|---|---|---|---|---|---|---|---|"]
    meta_keys = {"baselines", "spike_p99", "manifest"}
    for key, m in clean.items():
        if key in meta_keys:
            continue
        t = m["test"]["overall"]
        lines.append(f"| {key} | {t['mae']:.2f} | {t['mse']:.2f} | {t['rmse']:.2f} | "
                     f"{t['r2']:.4f} | {t['bias']:.2f} | {t['max_abs_err']:.2f} | {t['n']} |")
    for key, t in clean["baselines"].items():
        lines.append(f"| {key} | {t['mae']:.2f} | {t['mse']:.2f} | {t['rmse']:.2f} | "
                     f"{t['r2']:.4f} | {t['bias']:.2f} | {t['max_abs_err']:.2f} | {t['n']} |")
    (directory / "metrics.md").write_text("\n".join(lines))

    plot_curves(directory)

    print(f"RUN {version} done. artifacts: {directory}")
    print(json.dumps(clean, indent=2, default=str)[:4000])
    return clean


if __name__ == "__main__":
    main()
