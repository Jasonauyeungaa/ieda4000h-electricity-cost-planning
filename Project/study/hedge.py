"""Evaluate a fixed-volume wholesale contract on already verified schedules."""

from ai_compute.checker import evaluate_scenarios


def compare(packet, plans, forecast, observed, convert, strike, coverage):
    site = packet['site']
    hedged_kw = site['it_base_load_kw'] * site['pue'] * coverage
    volume_per_slot = hedged_kw * site['interval_minutes'] / 60 / 1000
    scenarios = {
        'Forecast': forecast,
        'Prices fall by A$50/MWh': [p - 50 for p in forecast],
        'Prices rise by A$50/MWh': [p + 50 for p in forecast],
        '16:00–20:00 NEM spike (+A$300/MWh)': [
            p + 300 if 16 <= int(slot['start'].split(':')[0]) < 20 else p
            for p, slot in zip(forecast, packet['tariff']['table'], strict=True)
        ],
    }
    if all(price is not None for price in observed):
        scenarios['Observed replay'] = observed

    rates = [(name, convert(prices)) for name, prices in scenarios.items()]
    baseline, optimized = [
        evaluate_scenarios(packet, rates, row['plan'])['scenario_costs']
        for row in plans
    ]
    rows = []
    for early, scheduled in zip(baseline, optimized, strict=True):
        name = early['scenario']
        settlement = sum((price - strike) * volume_per_slot for price in scenarios[name])
        scheduled_cost = scheduled['cost_currency']
        rows.append({
            'scenario': name,
            'start_early_aud': early['cost_currency'],
            'schedule_aud': scheduled_cost,
            'hedge_payoff_aud': settlement,
            'schedule_and_hedge_aud': scheduled_cost - settlement,
        })
    return {
        'strike_aud_mwh': strike,
        'coverage': coverage,
        'hedged_kw': hedged_kw,
        'hedged_mwh': volume_per_slot * len(forecast),
        'scenarios': rows,
        'note': 'Hypothetical contract for difference on NSW1 wholesale prices over the planning horizon. '
                'A fixed volume settles in each half-hour against the assumed strike. '
                'The retail tariff has a multiplier and floor, so some exposure remains. '
                'Stress cases have no assigned probabilities. Fees and margin are excluded.',
    }
