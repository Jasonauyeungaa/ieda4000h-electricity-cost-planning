"""Reprice the schedules from all five models on the original six test dates."""
import json
from statistics import mean, median
from tempfile import TemporaryDirectory
from pathlib import Path

from . import core, workbench


def main():
    outputs = core.DATA / 'outputs'
    dates = json.loads((outputs / 'evaluation_summary.json').read_text())['dates']
    by_day = {}
    with TemporaryDirectory(dir=outputs) as work:
        for day in dates:
            result = workbench.run_case({'day': day, 'scenario': 'standard', 'tariff': 'linked'},
                                        output_dir=Path(work))
            rows = result['comparison']['outcomes']
            if any(row['n'] != 48 or row['saving'] is None or row['check'] != 'PASS' for row in rows):
                raise ValueError(f'Incomplete or unverified replay: {day}')
            by_day[day] = rows
    methods = []
    for model in workbench.MODELS[:-1]:
        rows = [next(row for row in cases if row['method'] == model) for cases in by_day.values()]
        savings = [row['saving'] for row in rows]
        methods.append({'model': model, 'mean_mae': mean(row['mae'] for row in rows),
                        'mean_saving': mean(savings), 'median_saving': median(savings),
                        'cheaper': sum(value > 1e-7 for value in savings),
                        'more_expensive': sum(value < -1e-7 for value in savings)})
    result = {'dates': dates, 'scenario': 'standard', 'tariff': 'linked',
              'methods': methods, 'by_day': by_day}
    path = outputs / 'planning_model_evaluation.json'
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(path)
    for method in methods:
        print(method)


if __name__ == '__main__':
    main()
