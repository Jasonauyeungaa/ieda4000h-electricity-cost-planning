"""Run the bundled forecast models against the local data snapshot."""

import argparse
import json
from pathlib import Path

import forecast_engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--model', default='lstm',
                        choices=['auto', 'fnn', 'cnn', 'lstm', 'prev-day', 'prev-week'])
    args = parser.parse_args()
    data = Path(__file__).resolve().parents[2] / 'Data'
    result = forecast_engine.execute(
        {**vars(args), 'include_demand': True},
        output_dir=data / 'imports',
        demand_dir=data / 'outputs',
    )
    print(json.dumps({
        'file': f'imports/forecast_{result["id"]}.json',
        'demand_file': str(Path(result['demand_file']).relative_to(data)),
        'demand': result['demand'],
    }, allow_nan=False))


if __name__ == '__main__':
    main()
