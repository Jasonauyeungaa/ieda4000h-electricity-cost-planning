"""Connect the audited AEMO archive and existing model to the browser workbench."""
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
from functools import lru_cache
from zipfile import ZipFile

from . import core
from run_case import build_forecast

PREDICTION = core.DATA / 'prediction'
PROCESSED = PREDICTION / 'processed'
ARCHIVE = PREDICTION / 'raw' / 'aemo_nsw1_monthly.zip'
FIXTURES = core.DATA / 'outputs' / 'fixtures_20261114'
MODELS = ('lstm', 'cnn', 'fnn', 'prev-day', 'prev-week', 'auto')


@lru_cache(maxsize=1)
def manifest():
    return json.loads((PROCESSED / 'history_manifest.json').read_text())


def date_value(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('Choose a date in YYYY-MM-DD format.')
    return dt.date.fromisoformat(value)


def bounds():
    history = manifest()
    end = dt.date.fromisoformat(history['last_end'][:10])
    return {'first': history['first_start'][:10], 'last': (end - dt.timedelta(days=1)).isoformat(),
            'months': history['months'], 'intervals': history['halfhours']}


def market_day(value):
    day = date_value(value)
    filename = f'PRICE_AND_DEMAND_{day:%Y%m}_NSW1.csv'
    entry = next((item for item in manifest()['files'] if item['file'] == filename), None)
    if entry is None:
        raise ValueError('This date is outside the bundled AEMO archive.')
    if not ARCHIVE.is_file():
        raise ValueError('The AEMO ZIP is missing. Restore Data/prediction/raw/aemo_nsw1_monthly.zip.')
    with ZipFile(ARCHIVE) as archive:
        original = archive.read(filename)
    digest = hashlib.sha256(original).hexdigest()
    if digest != entry['sha256']:
        raise ValueError('The AEMO source file did not match the project manifest.')
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time())
    prefix = day.strftime('%Y/%m/%d')
    end_row = (day + dt.timedelta(days=1)).strftime('%Y/%m/%d 00:00:00')
    source = csv.DictReader(io.StringIO(original.decode('utf-8-sig')))
    raw = [row for row in source if (row['SETTLEMENTDATE'].startswith(prefix) and
           row['SETTLEMENTDATE'] != prefix + ' 00:00:00') or row['SETTLEMENTDATE'] == end_row]
    raw.sort(key=lambda row: row['SETTLEMENTDATE'])
    with gzip.open(PROCESSED / 'history_30min.csv.gz', 'rt', newline='') as stream:
        processed = []
        for row in csv.DictReader(stream):
            if row['timestamp'].startswith(value):
                processed.append(row)
            elif row['timestamp'] >= end.isoformat(sep=' '):
                break
    if not processed:
        raise ValueError('No complete half-hour market observations for this date.')
    return {'day': value, 'file': filename, 'sha256': digest, 'raw': raw, 'processed': processed,
            'source_minutes': entry['source_minutes'], 'range': bounds()}


def reference_case():
    folder = FIXTURES / '2026-10-22_standard'
    result = json.loads(next(folder.glob('schedule_*.json')).read_text())
    return {'run': result, 'comparison': None, 'recorded': True, 'range': bounds(),
            'input_file': next(folder.glob('forecast_*.json')).relative_to(core.DATA).as_posix()}


def run_case(body, log=None, stage=None, output_dir=None):
    log = log or (lambda message: None)
    stage = stage or (lambda name: None)
    day = date_value(body['day'])
    model = body.get('model', 'lstm')
    if model not in MODELS:
        raise ValueError('Choose a supported price model.')
    archive = bounds()
    if not '2025-01-01' <= day.isoformat() <= archive['last']:
        raise ValueError(f"Choose a forecast date from 2025-01-01 to {archive['last']}.")
    scenario, tariff = body.get('scenario', 'standard'), body.get('tariff', 'linked')
    if scenario not in ('standard', 'more-work', 'earlier') or tariff not in ('linked', 'flat'):
        raise ValueError('Choose a supported workload and tariff.')
    export_dir = output_dir or core.DATA / 'outputs' / 'workbench'
    stage('forecast')
    export, forecast, chosen = build_forecast(day.isoformat(), model, 'full', 'FG2', export_dir)
    mae = next((row['mae'] for row in forecast['metrics'] if row['method'] == chosen), None)
    log('forecast: %s model %s version %s, MAE %s AUD/MWh' % (
        day.isoformat(), chosen, export['model_version'],
        'n/a' if mae is None else '%.4f' % mae))
    path = export_dir / f"forecast_{export['export_id']}.json"
    relative = path.relative_to(core.DATA).as_posix()
    run = core.solve(relative, scenario, tariff, hedge_strike=body.get('hedge_strike'),
                     hedge_coverage=body.get('hedge_coverage', 0.5), out_dir=export_dir,
                     stage=stage, log=log)
    stage('compare')
    outcomes = compare_forecasts(export, forecast, run)
    log(f'compare: repriced {len(outcomes)} LP schedules with the same observed prices')
    log('output: ' + export_dir.as_posix() + '/')
    return {'run': run, 'comparison': {
        'model': chosen, 'predictions': forecast['predictions'], 'metrics': forecast['metrics'], 'outcomes': outcomes,
    }, 'recorded': False}


def compare_forecasts(export, forecast, selected_run):
    """Apply every method to the same job constraints and observed-price replay."""
    rows = []
    for metric in forecast['metrics']:
        method = metric['method']
        if method == export['model']:
            result = selected_run
        else:
            candidate = {
                **export, 'model': method,
                'slots': [{**slot, 'price': price} for slot, price in
                          zip(export['slots'], forecast['predictions'][method], strict=True)],
            }
            result = core.run_export(candidate, None, None, selected_run['scenario'],
                                     selected_run['tariff'], save=False)
        early, lp = result['plans']
        rows.append({**metric, 'budget': lp['forecast_cost'],
                     'replay': lp['actual_cost'], 'start_early_replay': early['actual_cost'],
                     'saving': result['actual_saving'], 'check': lp['check']['verdict']})
    return rows


# ---------------------------------------------------------------------------
# Frozen demo cases, provenance and offline replay.
#
# Fixtures are always resolved by FOLDER plus GLOB, never by exact filename and never by
# export_id, because export_id is timestamp-derived and changes on every regeneration.
# ---------------------------------------------------------------------------

CASES = (('2026-10-22', 'standard'), ('2026-09-17', 'more-work'))

BASE_GRID_KW = 10.0        # 8 kW IT load x PUE 1.25
BASE_KWH = BASE_GRID_KW * 24.0
WEIGHTS = PREDICTION / 'outputs' / 'm3'


def _rates(prices, tariff):
    if tariff == 'flat':
        return [0.30] * len(prices)
    return [max(float(p) / 1000 * 1.9, 0.001) for p in prices]


def _movable_cost(rows, rates):
    """Cost of the movable (GPU) energy only: grid draw above the immovable base load."""
    return sum(rates[i] * (row['grid_power_kw'] - BASE_GRID_KW) * row['interval_hours']
               for i, row in enumerate(rows))


def _case_folder(day, scenario):
    folder = FIXTURES / f'{day}_{scenario}'
    if not folder.is_dir():
        raise ValueError(f'No frozen fixture for {day} / {scenario}.')
    return folder


def case_summary(day, scenario):
    """Read one frozen fixture and derive every reported figure from the schedule itself."""
    folder = _case_folder(day, scenario)
    run = json.loads(next(folder.glob('schedule_*.json')).read_text())
    export = json.loads(next(folder.glob('forecast_*.json')).read_text())

    asap, lp = run['plans'][0], run['plans'][1]
    tariff = run['tariff']
    forecast_rates = _rates([s['price'] for s in export['slots']], tariff)
    actual_prices = export.get('evaluation_actuals') or [None] * 48
    have_actuals = all(p is not None for p in actual_prices)
    actual_rates = _rates(actual_prices, tariff) if have_actuals else None

    rows_asap, rows_lp = asap['check']['schedule'], lp['check']['schedule']
    total_kwh = lp['check']['summary']['total_grid_energy_kwh']
    gpu_kwh = total_kwh - BASE_KWH

    gpu_fc_asap = _movable_cost(rows_asap, forecast_rates)
    gpu_fc_lp = _movable_cost(rows_lp, forecast_rates)

    summary = {
        'day': day, 'scenario': scenario, 'tariff': tariff,
        'run_id': run['run_id'],
        'export': {
            'model': export['model'], 'model_version': export['model_version'],
            'schema': export['schema'], 'export_id': export['export_id'],
            'generated_at': export['generated_at'], 'kind': export['kind'],
            'input_hash': export.get('input_hash'), 'weight_sha256': export.get('weight_sha256'),
        },
        'forecast_priced': {
            'asap_aud': asap['forecast_cost'], 'lp_aud': lp['forecast_cost'],
            'saving_aud': run['predicted_saving'],
            'saving_pct': 100.0 * run['predicted_saving'] / asap['forecast_cost'] if asap['forecast_cost'] else 0.0,
        },
        'observed_priced': {
            'asap_aud': asap.get('actual_cost'), 'lp_aud': lp.get('actual_cost'),
            'saving_aud': run.get('actual_saving'),
            'saving_pct': (100.0 * run['actual_saving'] / asap['actual_cost']
                           if run.get('actual_saving') is not None and asap.get('actual_cost') else None),
        },
        'gpu_hours': lp['check']['summary']['total_gpu_hours'],
        'total_kwh': total_kwh,
        'movable_energy_kwh': gpu_kwh,
        'movable_energy_share_pct': 100.0 * gpu_kwh / total_kwh if total_kwh else 0.0,
        'movable_cost': {
            'asap_aud': gpu_fc_asap, 'lp_aud': gpu_fc_lp,
            'asap_share_pct': 100.0 * gpu_fc_asap / asap['forecast_cost'] if asap['forecast_cost'] else 0.0,
            'lp_share_pct': 100.0 * gpu_fc_lp / lp['forecast_cost'] if lp['forecast_cost'] else 0.0,
        },
        'checks': {'asap': asap['check']['verdict'], 'lp': lp['check']['verdict']},
        'peak_grid_kw': max(r['grid_power_kw'] for r in rows_lp),
    }
    if actual_rates is not None:
        gpu_ob_asap = _movable_cost(rows_asap, actual_rates)
        gpu_ob_lp = _movable_cost(rows_lp, actual_rates)
        summary['movable_cost_observed'] = {
            'asap_aud': gpu_ob_asap, 'lp_aud': gpu_ob_lp,
            'asap_share_pct': (100.0 * gpu_ob_asap / asap['actual_cost']) if asap.get('actual_cost') else None,
            'lp_share_pct': (100.0 * gpu_ob_lp / lp['actual_cost']) if lp.get('actual_cost') else None,
        }
    hedge = run.get('hedge')
    if hedge:
        scenarios = {r['scenario']: r for r in hedge['scenarios']}
        summary['hedge'] = {
            'strike_aud_mwh': hedge['strike_aud_mwh'], 'coverage': hedge['coverage'],
            'hedged_kw': hedge['hedged_kw'], 'hedged_mwh': hedge['hedged_mwh'],
            'forecast_payoff_aud': scenarios.get('Forecast', {}).get('hedge_payoff_aud'),
            'forecast_net_aud': scenarios.get('Forecast', {}).get('schedule_and_hedge_aud'),
            'observed_payoff_aud': scenarios.get('Observed replay', {}).get('hedge_payoff_aud'),
            'observed_net_aud': scenarios.get('Observed replay', {}).get('schedule_and_hedge_aud'),
        }
    return summary


def fixtures():
    """Both frozen demo cases, so favourable and adverse are always available together."""
    return {'cases': [case_summary(day, scenario) for day, scenario in CASES],
            'folder': FIXTURES.relative_to(core.DATA).as_posix()}


def fixture_for(day):
    for candidate_day, scenario in CASES:
        if candidate_day == day:
            return _case_folder(day, scenario)
    return None


def provenance(day):
    """Show where the numbers come from: history rows, weight identity, import provenance."""
    day = date_value(day)
    key = day.isoformat()
    history_path = PROCESSED / 'history_30min.csv.gz'
    if not history_path.is_file():
        raise ValueError('The processed history file is missing.')
    raw = history_path.read_bytes()
    end_dt = dt.datetime.combine(day + dt.timedelta(days=1), dt.time())
    rows = []
    with gzip.open(history_path, 'rt', newline='') as stream:
        for row in csv.DictReader(stream):
            if row['timestamp'].startswith(key):
                rows.append(row)
            elif row['timestamp'] >= end_dt.isoformat(sep=' '):
                break
    if not rows:
        raise ValueError('No half-hour history rows for this date.')

    folder = fixture_for(key)
    if folder is not None:
        export_path = next(folder.glob('forecast_*.json'))
        export = json.loads(export_path.read_text())
        source = export_path.relative_to(core.DATA).as_posix()
    else:
        export, source = None, None

    run_version = export['model_version'] if export else None
    weight_files = []
    if run_version and (WEIGHTS / run_version / 'weights').is_dir():
        for path in sorted((WEIGHTS / run_version / 'weights').glob('*.pt')):
            blob = path.read_bytes()
            weight_files.append({'name': path.name, 'bytes': len(blob),
                                 'sha256': hashlib.sha256(blob).hexdigest()})

    return {
        'day': key,
        'history': {
            'file': history_path.relative_to(core.DATA).as_posix(),
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'rows_for_day': len(rows),
            'first': rows[0]['timestamp'], 'last': rows[-1]['timestamp'],
            'sample': rows[:3],
            'total_rows': manifest()['halfhours'],
        },
        'weights': {'run': run_version, 'folder': f'prediction/outputs/m3/{run_version}/weights',
                    'files': weight_files},
        'import': (None if export is None else {
            'file': source, 'schema': export['schema'], 'export_id': export['export_id'],
            'model': export['model'], 'model_version': export['model_version'],
            'generated_at': export['generated_at'], 'kind': export['kind'],
            'input_hash': export.get('input_hash'), 'weight_sha256': export.get('weight_sha256'),
        }),
    }


def replay_day(day):
    """The full set of observed actuals for a day. This is the offline replay series only."""
    value = date_value(day).isoformat()
    market = market_day(value)
    rows = market['processed']
    prices = [float(row['price']) for row in rows]
    return {
        'day': value,
        'source': 'observed_history',
        'live_feed': 'separate tab',
        'note': 'Historical replay of finalized observed prices. Use Live AEMO for updated market reports.',
        'count': len(prices),
        'actuals': prices,
        'stamps': [row['timestamp'] for row in rows],
        'stats': {'min': min(prices), 'max': max(prices),
                  'mean': sum(prices) / len(prices),
                  'over_1000': sum(1 for p in prices if p > 1000)},
    }


def planning_evaluation():
    """Return the stored same-input planning benchmark."""
    path = core.DATA / 'outputs' / 'planning_model_evaluation.json'
    if not path.is_file():
        raise ValueError('The six-date comparison has not been computed.')
    return json.loads(path.read_text())
