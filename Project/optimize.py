"""Run and verify AI workload scheduling from a terminal."""
import argparse
import json

from study.core import DATA, solve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--scenario', default='standard',
                        choices=['standard', 'more-work', 'earlier'])
    parser.add_argument('--tariff', default='linked', choices=['linked', 'flat'])
    parser.add_argument('--hedge-strike', type=float, metavar='AUD/MWh',
                        help='enable the hypothetical wholesale hedge')
    parser.add_argument('--hedge-coverage', type=float, default=0.5,
                        help='fraction of base grid load covered (default: 0.5)')
    args = parser.parse_args()
    try:
        result = solve(args.input, args.scenario, args.tariff,
                       hedge_strike=args.hedge_strike, hedge_coverage=args.hedge_coverage)
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    summary = {
        'output': str(DATA / 'outputs' / (result['run_id'] + '.json')),
        'predicted_saving': result['predicted_saving'],
        'actual_saving': result['actual_saving'],
        'checks': [plan['check']['verdict'] for plan in result['plans']],
    }
    if 'hedge' in result:
        summary['hedge'] = result['hedge']
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
