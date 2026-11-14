"""Regional demand forecasting using the course's existing neural training code."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json

import numpy as np
import pandas as pd
import torch

from course_models.history import DATA, load
from course_models.models import build_model
from course_models.train_m3 import sample, tensors, train_model, model_forward, metrics

DIRECTORY = DATA / 'outputs' / 'demand_v1'
KINDS = ('fnn', 'cnn', 'lstm')


def demand_view(frame):
    # The shared sampler targets its first channel. This named projection makes
    # regional MW the target and first lag channel, with lagged price second.
    return frame.rename(columns={'demand': 'price', 'price': 'demand'})


def train(epochs=30):
    torch.set_num_threads(2)
    torch.manual_seed(0)
    np.random.seed(0)
    frame = demand_view(load())
    records = []
    for origin in pd.date_range('2016-11-08', '2026-10-31', freq='D'):
        row = sample(frame, origin)
        if row is not None:
            records.append((origin, *row))
    origins = pd.DatetimeIndex([row[0] for row in records])
    x, calendar, weekly, y = [np.stack([row[i] for row in records]) for i in range(1, 5)]
    train_indices = np.flatnonzero(origins < '2024-01-01')
    validation_indices = np.flatnonzero((origins >= '2024-01-01') & (origins < '2025-01-01'))
    test_indices = np.flatnonzero(origins >= '2025-01-01')
    scale = {
        'x_mean': x[train_indices].mean(axis=(0, 1)).tolist(),
        'x_std': np.maximum(x[train_indices].std(axis=(0, 1)), 1e-6).tolist(),
        'y_mean': float(y[train_indices].mean()),
        'y_std': float(max(y[train_indices].std(), 1e-6)),
    }
    X, extra = tensors(x, calendar, weekly, 'FG2', scale)
    Y = torch.tensor((y - scale['y_mean']) / scale['y_std'], dtype=torch.float32)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    results = {}
    predictions = []
    for kind in KINDS:
        print('Training demand', kind, flush=True)
        net, state, prediction, validation_mae, curve = train_model(
            kind, 'FG2', X, extra, Y, y, train_indices, validation_indices,
            test_indices, scale, epochs, 0, verbose=False)
        weight = DIRECTORY / (kind + '.pt')
        torch.save(state, weight)
        results[kind] = {
            'validation_mae': validation_mae,
            'test': metrics(prediction, y[test_indices]),
            'weight_sha256': hashlib.sha256(weight.read_bytes()).hexdigest(),
            'curve': curve,
        }
        for day, actual, predicted in zip(origins[test_indices], y[test_indices], prediction):
            for slot, (a, p) in enumerate(zip(actual, predicted)):
                predictions.append({'method': kind, 'target_start': str(day + pd.Timedelta(minutes=30 * slot)),
                                    'actual_mw': float(a), 'predicted_mw': float(p)})
    previous_day = np.stack([frame.reindex(pd.date_range(day - pd.Timedelta(days=1), periods=48, freq='30min')).price.to_numpy()
                             for day in origins[test_indices]])
    results['prev-day'] = {'test': metrics(previous_day, y[test_indices])}
    results['prev-week'] = {'test': metrics(weekly[test_indices], y[test_indices])}
    selected = min(KINDS, key=lambda kind: results[kind]['validation_mae'])
    config = {
        'target': 'NSW1 regional demand', 'unit': 'MW', 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'selected_model': selected, 'epochs_max': epochs, 'seed': 0, 'scalers': scale,
        'features': 'Prior 48 hours of regional demand and price plus calendar. Observations precede origin.',
        'horizon': '48 half-hours', 'train': '2016-11-08 to 2023-12-31',
        'validation': '2024', 'test': '2025-01-01 to 2026-10-31',
        'test_origins': len(test_indices), 'test_targets': int(y[test_indices].size),
        'history_sha256': hashlib.sha256((DATA / 'processed/history_30min.csv.gz').read_bytes()).hexdigest(),
        'limitations': 'One seed and one chronological split. Aggregate regional demand includes many uses. Individual AI, data-centre and EV contributions require separately measured inputs.',
    }
    (DIRECTORY / 'config.json').write_text(json.dumps(config, indent=2, allow_nan=False))
    (DIRECTORY / 'metrics.json').write_text(json.dumps(results, indent=2, allow_nan=False))
    pd.DataFrame(predictions).to_csv(DIRECTORY / 'predictions.csv.gz', index=False, compression='gzip')
    pd.DataFrame([{'method': kind, **row['test'], 'validation_mae': row.get('validation_mae')} for kind, row in results.items()]).to_csv(DIRECTORY / 'metrics.csv', index=False)
    print(json.dumps({'selected_model': selected, 'metrics': {k: v['test'] for k, v in results.items()}}, indent=2), flush=True)


def predict(day='2026-03-11', frame=None):
    config = json.loads((DIRECTORY / 'config.json').read_text())
    results = json.loads((DIRECTORY / 'metrics.json').read_text())
    origin = pd.Timestamp(day)
    if origin.tzinfo is not None or origin != origin.normalize() or origin < pd.Timestamp('2025-01-01'):
        raise ValueError('Choose a daily NEM origin in 2025 or later')
    frame = demand_view(load() if frame is None else frame)
    row = sample(frame, origin, targets=False)
    if row is None:
        raise ValueError('Incomplete prior demand and price inputs for this origin')
    prior, calendar, weekly, _ = row
    X, extra = tensors(prior[None], calendar[None], weekly[None], 'FG2', config['scalers'])
    predictions = {}
    torch.set_num_threads(2)
    for kind in KINDS:
        weight = DIRECTORY / (kind + '.pt')
        if hashlib.sha256(weight.read_bytes()).hexdigest() != results[kind]['weight_sha256']:
            raise ValueError('Demand model weight hash mismatch')
        net = build_model(kind, 2, extra.shape[1])
        net.load_state_dict(torch.load(weight, map_location='cpu', weights_only=True))
        net.eval()
        with torch.no_grad():
            value = model_forward(net, kind, X, extra).numpy()[0]
        predictions[kind] = (value * config['scalers']['y_std'] + config['scalers']['y_mean']).astype(float).tolist()
    predictions['prev-day'] = frame.reindex(pd.date_range(origin - pd.Timedelta(days=1), periods=48, freq='30min')).price.tolist()
    predictions['prev-week'] = weekly.astype(float).tolist()
    targets = pd.date_range(origin, periods=48, freq='30min')
    actual = frame.reindex(targets).price.to_numpy(float)
    mask = np.isfinite(actual)
    report = {
        'target': config['target'], 'unit': 'MW', 'origin': str(origin) + '+10:00',
        'labels': [t.isoformat() + '+10:00' for t in targets],
        'predictions': predictions, 'actual': [float(v) if np.isfinite(v) else None for v in actual],
        'metrics': [{'method': kind, **metrics(np.array(values)[mask], actual[mask])} for kind, values in predictions.items()] if mask.any() else [],
        'test_metrics': [{'method': kind, **v['test']} for kind, v in results.items()],
        'config': config, 'executed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'kind': 'historical_replay',
    }
    return report


def main():
    parser = argparse.ArgumentParser(description='Train or run regional demand forecasting')
    parser.add_argument('--train', action='store_true')
    parser.add_argument('--epochs', type=int, default=30)
    parser.add_argument('--day', default='2026-03-11')
    parser.add_argument('--model-dir', type=Path)
    args = parser.parse_args()
    global DIRECTORY
    if args.model_dir:
        DIRECTORY = args.model_dir.resolve()
    elif args.train:
        DIRECTORY = DATA / 'outputs' / dt.datetime.now(dt.timezone.utc).strftime('demand_%Y%m%dT%H%M%S%fZ')
    if args.train:
        train(args.epochs)
    result = predict(args.day)
    destination = DATA / 'exports' / ('demand_' + args.day + '_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, allow_nan=False))
    print(destination)


if __name__ == '__main__':
    main()
