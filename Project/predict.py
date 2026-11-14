"""Run the bundled price and demand forecasts."""

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path


def generate(day, model='lstm'):
    if not isinstance(day, str) or len(day) != 10:
        raise ValueError('Choose a date in YYYY-MM-DD format')
    if dt.date.fromisoformat(day).isoformat() != day:
        raise ValueError('Choose a date in YYYY-MM-DD format')
    if model not in ('auto', 'fnn', 'cnn', 'lstm', 'prev-day', 'prev-week'):
        raise ValueError('Choose a supported price model')
    script = Path(__file__).resolve().parent / 'prediction/run.py'
    result = subprocess.run([sys.executable, str(script), '--day', day, '--model', model],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        error = result.stderr.strip().splitlines()
        raise ValueError(error[-1] if error else 'Forecast execution failed')
    return json.loads(result.stdout)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--model', default='lstm')
    args = parser.parse_args()
    try:
        result = generate(args.day, args.model)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ('file', 'demand_file')}, indent=2))
