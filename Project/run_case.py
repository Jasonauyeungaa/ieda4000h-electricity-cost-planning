"""Run the full electricity-cost chain for one day and workload with a single command.

One command does everything, from the Project folder:

    .venv/bin/python run_case.py --day 2026-10-22 --scenario standard --hedge-strike 100

The chain is: build (or load) the NSW1 forecast, run the start-early baseline, run the LP
optimiser, verify both plans with the independent checker, settle the optional contract for
difference against observed prices, and write every artefact to a timestamped folder under
Data/outputs/. No network access, no API key and no manual step are required.

The forecast is BUILT DURING THE RUN from the bundled model weights and the local price
history in Data/prediction. It is not downloaded. Use --input to replay a saved export
instead; that path is recorded as loaded_from_file in the summary.
"""
import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from study import core

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / 'Data'
BASE_IT_KW = 8.0
PUE = 1.25
GRID_CAP_KW = 40.0
BASE_GRID_KW = BASE_IT_KW * PUE          # 10.0 kW immovable base load
BASE_KWH = BASE_GRID_KW * 24.0           # 240.0 kWh immovable energy per day


def rate_for(price, tariff):
    """Convert a wholesale price in AUD/MWh to a retail rate in AUD/kWh."""
    if tariff == 'flat':
        return 0.30
    return max(float(price) / 1000.0 * 1.9, 0.001)


def movable_cost(rows, rates):
    """Cost of the movable (GPU) energy only: grid power above the immovable base load."""
    return sum(rates[i] * (row['grid_power_kw'] - BASE_GRID_KW) * row['interval_hours']
               for i, row in enumerate(rows))


def build_forecast(day, model, window, group, out_dir):
    """Build the forecast from the bundled weights and local history, writing the export to out_dir."""
    sys.path.insert(0, str(HERE / 'prediction'))
    import forecast_engine

    frame = forecast_engine.market_frame()
    forecast = forecast_engine.predict(frame, day, window, group)
    chosen = model
    if chosen == 'auto':
        chosen = forecast_engine.experiments(window, group)['validation_choice']
    export = forecast_engine.make_export(forecast, chosen, output_dir=out_dir)
    return export, forecast, chosen


def summarise(result, export, forecast_meta, source, chosen_model):
    """Build the flat, human-readable summary required by the report."""
    plans = result['plans']
    asap, lp = plans[0], plans[1]
    forecast_prices = [row['price'] for row in export['slots']]
    actual_prices = export.get('evaluation_actuals') or [None] * 48
    have_actuals = all(p is not None for p in actual_prices)

    tariff = result['tariff']
    rows = lp['check']['schedule']
    fr = [rate_for(p, tariff) for p in forecast_prices]
    ar = [rate_for(p, tariff) for p in actual_prices] if have_actuals else None

    gpu_fc_asap = movable_cost(asap['check']['schedule'], fr)
    gpu_fc_lp = movable_cost(lp['check']['schedule'], fr)
    delta_fc = 100.0 * (1 - gpu_fc_lp / gpu_fc_asap) if gpu_fc_asap else 0.0
    delta_obs = None
    if ar is not None:
        gpu_ob_asap = movable_cost(asap['check']['schedule'], ar)
        gpu_ob_lp = movable_cost(lp['check']['schedule'], ar)
        delta_obs = 100.0 * (1 - gpu_ob_lp / gpu_ob_asap) if gpu_ob_asap else 0.0

    total_kwh = lp['check']['summary']['total_grid_energy_kwh']
    gpu_kwh = total_kwh - BASE_KWH
    max_grid_kw = max(row['grid_power_kw'] for row in rows)

    per_job = {}
    for job_id, info in lp['check']['per_job'].items():
        per_job[job_id] = {
            'required_gpu_hours': info['required_gpu_hours'],
            'delivered_gpu_hours': info['done_gpu_hours'],
            'window_hours': info['window_hours'],
            'within_window': abs(info['done_gpu_hours'] - info['required_gpu_hours']) < 1e-6,
        }

    forecast_mae = None
    if forecast_meta:
        forecast_mae = next((m['mae'] for m in forecast_meta.get('metrics', [])
                             if m['method'] == chosen_model), None)

    saving_pct_fc = 100.0 * result['predicted_saving'] / asap['forecast_cost'] if asap['forecast_cost'] else 0.0
    saving_pct_ob = None
    if result.get('actual_saving') is not None and asap.get('actual_cost'):
        saving_pct_ob = 100.0 * result['actual_saving'] / asap['actual_cost']

    summary = {
        'day': export['forecast_origin'][:10],
        'scenario': result['scenario'],
        'tariff': tariff,
        'forecast_source': source,
        'model': chosen_model,
        'model_version': export.get('model_version'),
        'export_id': export.get('export_id'),
        'run_id': result['run_id'],
        'forecast_mae_aud_per_mwh': forecast_mae,
        'gpu_hours_delivered': lp['check']['summary']['total_gpu_hours'],
        'total_grid_energy_kwh': total_kwh,
        'movable_energy_kwh': gpu_kwh,
        # Energy share and cost share are DIFFERENT numbers and must never be conflated:
        # the tariff varies by interval, so movable energy does not carry the average rate.
        'movable_energy_share_pct': 100.0 * gpu_kwh / total_kwh if total_kwh else 0.0,
        'movable_cost': {
            'asap_aud': gpu_fc_asap,
            'lp_aud': gpu_fc_lp,
            'asap_share_pct': 100.0 * gpu_fc_asap / asap['forecast_cost'] if asap['forecast_cost'] else 0.0,
            'lp_share_pct': 100.0 * gpu_fc_lp / lp['forecast_cost'] if lp['forecast_cost'] else 0.0,
        },
        'forecast_priced': {
            'asap_cost_aud': asap['forecast_cost'],
            'lp_cost_aud': lp['forecast_cost'],
            'saving_aud': result['predicted_saving'],
            'saving_pct_of_bill': saving_pct_fc,
        },
        'observed_priced': {
            'asap_cost_aud': asap.get('actual_cost'),
            'lp_cost_aud': lp.get('actual_cost'),
            'saving_aud': result.get('actual_saving'),
            'saving_pct_of_bill': saving_pct_ob,
        },
        'delta_forecast_pct': delta_fc,
        'delta_observed_pct': delta_obs,
        'checker_verdicts': {'asap': asap['check']['verdict'], 'lp': lp['check']['verdict']},
        'checks': {
            'all_jobs_within_window': all(v['within_window'] for v in per_job.values()),
            'grid_power_cap_kw': GRID_CAP_KW,
            'max_grid_power_kw': max_grid_kw,
            'grid_power_within_cap': max_grid_kw <= GRID_CAP_KW + 1e-9,
            'per_job': per_job,
        },
    }
    if 'hedge' in result:
        hedge = result['hedge']
        scenarios = {row['scenario']: row for row in hedge['scenarios']}
        summary['hedge'] = {
            'strike_aud_per_mwh': hedge['strike_aud_mwh'],
            'coverage': hedge['coverage'],
            'hedged_kw': hedge['hedged_kw'],
            'hedged_mwh': hedge['hedged_mwh'],
            'forecast_payoff_aud': scenarios.get('Forecast', {}).get('hedge_payoff_aud'),
            'forecast_net_cost_aud': scenarios.get('Forecast', {}).get('schedule_and_hedge_aud'),
            'observed_payoff_aud': scenarios.get('Observed replay', {}).get('hedge_payoff_aud'),
            'observed_net_cost_aud': scenarios.get('Observed replay', {}).get('schedule_and_hedge_aud'),
        }
    return summary


def render_text(s):
    """Render the summary as plain text for a terminal or a report appendix."""
    lines = []
    add = lines.append
    add('=' * 72)
    add(f"  {s['day']}  |  scenario {s['scenario']}  |  tariff {s['tariff']}")
    add('=' * 72)
    add(f"  forecast source : {s['forecast_source']} ({s['model']}, {s['model_version']})")
    add(f"  run id          : {s['run_id']}")
    add('')
    add(f"  GPU-hours delivered : {s['gpu_hours_delivered']:.2f}")
    add(f"  total energy        : {s['total_grid_energy_kwh']:.4f} kWh")
    add(f"  movable energy      : {s['movable_energy_kwh']:.4f} kWh  "
        f"({s['movable_energy_share_pct']:.2f}% of ENERGY)")
    mc = s['movable_cost']
    add(f"  movable cost        : A${mc['asap_aud']:.4f} start-early ({mc['asap_share_pct']:.2f}% of bill)"
        f"  |  A${mc['lp_aud']:.4f} optimised ({mc['lp_share_pct']:.2f}% of bill)")
    add('')
    fp, op = s['forecast_priced'], s['observed_priced']
    add('  FORECAST-PRICED (the LP objective)')
    add(f"    start-early : A${fp['asap_cost_aud']:.4f}")
    add(f"    optimised   : A${fp['lp_cost_aud']:.4f}")
    add(f"    saving      : A${fp['saving_aud']:.4f}  ({fp['saving_pct_of_bill']:.4f}% of bill)")
    add('')
    add('  OBSERVED-PRICED (what the bill actually did)')
    if op['asap_cost_aud'] is None:
        add('    pending: observed prices are incomplete for this date')
    else:
        add(f"    start-early : A${op['asap_cost_aud']:.4f}")
        add(f"    optimised   : A${op['lp_cost_aud']:.4f}")
        add(f"    saving      : A${op['saving_aud']:.4f}  ({op['saving_pct_of_bill']:.4f}% of bill)")
    add('')
    add(f"  delta (movable-cost cut) : forecast {s['delta_forecast_pct']:.2f}%"
        + (f"  |  observed {s['delta_observed_pct']:.2f}%" if s['delta_observed_pct'] is not None else ''))
    add('')
    add(f"  checker verdicts : start-early {s['checker_verdicts']['asap']}  |  "
        f"optimised {s['checker_verdicts']['lp']}")
    add(f"  deadline windows : {'all jobs satisfied' if s['checks']['all_jobs_within_window'] else 'VIOLATION'}")
    add(f"  grid power       : {s['checks']['max_grid_power_kw']:.2f} kW of "
        f"{s['checks']['grid_power_cap_kw']:.1f} kW cap  ->  "
        f"{'OK' if s['checks']['grid_power_within_cap'] else 'EXCEEDED'}")
    for job_id, info in s['checks']['per_job'].items():
        add(f"    {job_id:<10} {info['delivered_gpu_hours']:.2f} of "
            f"{info['required_gpu_hours']:.2f} GPU-h within {info['window_hours']:.1f} h window")
    if 'hedge' in s:
        h = s['hedge']
        add('')
        add(f"  HEDGE  strike A${h['strike_aud_per_mwh']:.0f}/MWh, coverage {h['coverage']:.0%}, "
            f"{h['hedged_kw']:.2f} kW = {h['hedged_mwh']:.4f} MWh/day")
        add(f"    forecast scenario : payoff A${h['forecast_payoff_aud']:+.4f}  "
            f"-> net cost A${h['forecast_net_cost_aud']:.4f}")
        if h['observed_payoff_aud'] is not None:
            add(f"    observed replay   : payoff A${h['observed_payoff_aud']:+.4f}  "
                f"-> net cost A${h['observed_net_cost_aud']:.4f}")
    add('=' * 72)
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--day', help='forecast origin as YYYY-MM-DD; the forecast is built during the run')
    source.add_argument('--input', help='replay a saved export, path relative to Data/ (e.g. imports/forecast_x.json)')
    parser.add_argument('--scenario', default='standard', choices=['standard', 'more-work', 'earlier'])
    parser.add_argument('--tariff', default='linked', choices=['linked', 'flat'])
    parser.add_argument('--model', default='lstm', choices=['auto', 'fnn', 'cnn', 'lstm', 'prev-day', 'prev-week'])
    parser.add_argument('--window', default='full', choices=['full', 'recent'])
    parser.add_argument('--group', default='FG2', choices=['FG1', 'FG2', 'FG3'])
    parser.add_argument('--hedge-strike', type=float, metavar='AUD/MWh',
                        help='enable the hypothetical wholesale hedge at this strike')
    parser.add_argument('--hedge-coverage', type=float, default=0.5,
                        help='fraction of base grid load covered (default 0.5)')
    parser.add_argument('--out-root', default=None,
                        help='override the output root (default Data/outputs)')
    args = parser.parse_args()

    out_root = Path(args.out_root) if args.out_root else DATA / 'outputs'
    label = args.day or Path(args.input).stem.replace('forecast_', '')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_dir = out_root / f"run_{label}_{args.scenario}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    forecast_meta = None
    try:
        if args.day:
            export, forecast_meta, chosen = build_forecast(
                args.day, args.model, args.window, args.group, out_dir)
            sha = hashlib.sha256(
                json.dumps(export, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            source_label = f"built_during_run:{args.day}"
        else:
            export, sha = core.load(args.input)
            chosen = export.get('model', args.model)
            source_label = f"loaded_from_file:{args.input}"

        result = core.run_export(
            export, sha, source_label, args.scenario, args.tariff,
            hedge_strike=args.hedge_strike, hedge_coverage=args.hedge_coverage, out_dir=out_dir)
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))

    summary = summarise(result, export, forecast_meta, source_label, chosen)
    summary['output_dir'] = str(out_dir)
    summary['schedule_file'] = result.get('output_file')

    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False))
    text = render_text(summary)
    (out_dir / 'summary.txt').write_text(text + '\n')
    print(text)
    print(f"\nArtefacts written to {out_dir}")


if __name__ == '__main__':
    main()
