"""Public AEMO observations and as-published pre-dispatch planning inputs."""
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import math
import re
import urllib.request
from zipfile import BadZipFile, ZipFile

from . import core

NEM = dt.timezone(dt.timedelta(hours=10))
BASE = 'https://www.nemweb.com.au/Reports/CURRENT/'
DISPATCH = BASE + 'DispatchIS_Reports/'
PREDISPATCH = BASE + 'Predispatch_Reports/'
DISPATCH_NAME = re.compile(r'PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip', re.I)
PREDISPATCH_NAME = re.compile(r'PUBLIC_PREDISPATCH_\d{12}_(\d{14})_LEGACY\.zip', re.I)
LIVE_OUTPUTS = core.DATA / 'outputs' / 'live_market'


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def clock():
    return dt.datetime.now(NEM)


def market_stamp(value, pattern='%Y/%m/%d %H:%M:%S'):
    return dt.datetime.strptime(value, pattern).replace(tzinfo=NEM)


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'IEDA4000H-NSW1/1.0', 'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(8_000_001)
    except OSError as error:
        raise ValueError(f'AEMO feed unavailable: {error}') from error
    if len(raw) > 8_000_000:
        raise ValueError('AEMO download exceeded the expected size.')
    return raw


def latest_report(directory, pattern, now, parser):
    listing = fetch(directory).decode('utf-8', errors='replace')
    eligible = {}
    for match in pattern.finditer(listing):
        stamp = parser(match[1])
        if stamp <= now:
            eligible[match[0]] = stamp
    if not eligible:
        raise ValueError('No AEMO report was available at the current market time.')
    name = max(eligible, key=lambda key: (eligible[key], key))
    url = directory + name
    return fetch(url), url, eligible[name]


def report_rows(raw, wanted):
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            files = [file for file in archive.infolist() if file.filename.lower().endswith('.csv')]
            if len(files) != 1 or files[0].file_size > 25_000_000:
                raise ValueError('Unexpected AEMO report contents.')
            text = archive.read(files[0]).decode('utf-8-sig')
    except (BadZipFile, UnicodeError) as error:
        raise ValueError('AEMO returned an unreadable ZIP report.') from error
    headers = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5 or tuple(row[1:3]) not in wanted:
            continue
        key = tuple(row[1:4])
        if row[0] == 'I':
            headers[key] = row[4:]
        elif row[0] == 'D' and key in headers:
            record = dict(zip(headers[key], row[4:]))
            if record.get('REGIONID') == 'NSW1' and record.get('INTERVENTION') == '0':
                yield tuple(row[1:3]), record


def newest_per_time(rows, time_field, value_field, nonnegative=False):
    values = {}
    for row in rows:
        stamp = market_stamp(row[time_field])
        value = float(row[value_field])
        if not math.isfinite(value) or (nonnegative and value < 0):
            raise ValueError(f'Invalid {value_field} in AEMO report.')
        revision = int(row.get('RUNNO', '0'))
        if stamp not in values or revision > values[stamp][0]:
            values[stamp] = revision, value
    return {stamp: value for stamp, (_, value) in values.items()}


def observation():
    requested = clock()
    raw, url, file_stamp = latest_report(DISPATCH, DISPATCH_NAME, requested,
                                        lambda stamp: market_stamp(stamp, '%Y%m%d%H%M'))
    price_key = ('DISPATCH', 'PRICE')
    demand_key = ('DISPATCH', 'REGIONSUM')
    rows = list(report_rows(raw, {price_key, demand_key}))
    prices = newest_per_time((row for key, row in rows if key == price_key), 'SETTLEMENTDATE', 'RRP')
    demand = newest_per_time((row for key, row in rows if key == demand_key),
                             'SETTLEMENTDATE', 'TOTALDEMAND', nonnegative=True)
    matched = [stamp for stamp in prices.keys() & demand.keys() if stamp <= requested]
    if not matched:
        raise ValueError('No completed NSW1 Dispatch interval with both price and demand.')
    latest = max(matched)
    age = (requested - latest).total_seconds() / 60
    report = {'interval_end': latest.isoformat(), 'checked_at': requested.isoformat(),
              'report_label': file_stamp.isoformat(), 'age_minutes': round(age, 1), 'fresh': age <= 15,
              'price': prices[latest], 'demand': demand[latest], 'url': url,
              'sha256': hashlib.sha256(raw).hexdigest()}
    write_json(LIVE_OUTPUTS / 'latest_dispatch.json', report)
    return report


def predispatch_export():
    issued = clock()
    raw, url, published = latest_report(PREDISPATCH, PREDISPATCH_NAME, issued,
                                       lambda stamp: market_stamp(stamp, '%Y%m%d%H%M%S'))
    if issued - published > dt.timedelta(minutes=90):
        raise ValueError('AEMO Pre-dispatch is stale. Refresh before planning.')
    prices = newest_per_time((row for _, row in report_rows(raw, {('PDREGION', '')})), 'PERIODID', 'RRP')
    ready_time = clock() + dt.timedelta(minutes=1)
    origin = ready_time.replace(minute=(ready_time.minute // 30) * 30, second=0, microsecond=0)
    origin += dt.timedelta(minutes=30)
    step = dt.timedelta(minutes=30)
    ends = []
    for i in range(1, 49):
        end = origin + i * step
        if end not in prices:
            break
        ends.append(end)
    if len(ends) < 16:
        raise ValueError('The latest AEMO Pre-dispatch report covers fewer than eight continuous future hours.')
    manifest_hash = hashlib.sha256(raw).hexdigest()
    return core.validate({
        'schema': core.SCHEMA, 'export_id': f"{origin:%Y%m%dT%H%M}_{manifest_hash[:14]}",
        'region': 'NSW1', 'currency': 'AUD', 'price_unit': 'AUD/MWh', 'interval_minutes': 30,
        'model': 'aemo-predispatch', 'model_version': published.isoformat(), 'kind': 'live_aemo_predispatch',
        'generated_at': clock().isoformat(), 'forecast_origin': origin.isoformat(),
        'input_cutoff': published.isoformat(), 'source_url': url, 'source_sha256': manifest_hash,
        'slots': [{'target_start': (end - step).isoformat(), 'target_end': end.isoformat(),
                   'price': prices[end]} for end in ends],
        'evaluation_actuals': [None] * len(ends),
        'limitations': 'Future wholesale estimates from AEMO Pre-dispatch. Live demo jobs use shorter windows relative to horizon start.',
    })


def plan(body):
    export = predispatch_export()
    path = LIVE_OUTPUTS / f"forecast_{export['export_id']}.json"
    write_json(path, export)
    relative = path.relative_to(core.DATA).as_posix()
    run = core.solve(relative, body.get('scenario', 'standard'), body.get('tariff', 'linked'),
                     hedge_strike=body.get('hedge_strike'), hedge_coverage=body.get('hedge_coverage', 0.5),
                     out_dir=LIVE_OUTPUTS)
    return {'run': run, 'comparison': None, 'recorded': False, 'live': True}
