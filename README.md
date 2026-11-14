# Electricity Cost Planning for AI Computing

IEDA 4000H, HKUST · Jason Au-Yeung

This tool helps an operations manager turn tomorrow's GPU workload and a wholesale-linked tariff into a daily electricity budget. It compares an early-start schedule with a linear-programming schedule, verifies both independently and replays them at observed NSW wholesale prices. An optional fixed-volume contract for difference (CfD) shows how the same schedule would settle under the assumed hedge terms.

## Run the project

From the repository root with Python 3.12:

```sh
cd Project
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run_case.py --day 2026-10-22 --scenario standard --hedge-strike 100
.venv/bin/python -m unittest study.test_optimization
```

The model and solver were tested with Python 3.12.9, PuLP 3.3.2 (CBC), NumPy 2.5.4, pandas 3.1.1 and PyTorch 2.15.0 on macOS arm64. Installation requires network access. The cases run locally afterwards. On Windows, use the virtual environment's `Scripts/python.exe`.

The command builds a 48-slot LSTM forecast from the bundled history and weight files, then writes a timestamped directory in `Data/outputs`. `forecast_*.json` contains the price series and provenance. `schedule_*.json` contains both schedules and checker reports. `summary.json` and `summary.txt` show the budgets and settlement. Use `--out-root` to write elsewhere. The 22 October case should deliver 25 GPU-hours for each plan with two PASS verdicts. The start-early and LP budgets are A$49.6965 and A$49.5550. Repricing the same schedules gives A$55.8890 and A$55.6079.

Use `--day 2026-09-17 --scenario more-work` with the same strike to reproduce the second recorded case. To run from an included forecast instead of doing inference, use:

```sh
.venv/bin/python run_case.py --input outputs/fixtures_20261114/2026-10-22_standard/forecast_7bd4a665621df0c8aab77780.json --scenario standard --hedge-strike 100
```

## Reuse the Skill

Follow [compare-ai-electricity-cost](Project/skills/compare-ai-electricity-cost/SKILL.md). The changed example imports the 15 October 2026 forecast, completes 37.5 GPU-hours and returns two PASS verdicts:

```sh
.venv/bin/python optimize.py --input imports/forecast_aac8fe2222acaca756808516.json --scenario more-work --tariff linked
```

Inputs are versioned NSW1 forecast JSON with 48 continuous half-hours, market time UTC+10, price in AUD/MWh and available observed values for replay. Choose another valid forecast and case, then check job hours, capacity, grid power and cost. The import validator rejects malformed packets. If observed prices are incomplete, the historical replay remains pending. [The clean-copy run record](Data/outputs/skill_reuse_20261114_clean_copy.json) documents manual self-reproduction. [Evidence/](Evidence/) links native WorkSwarm tool calls to saved numerical output.

## Data and scope

AEMO is the source and author of the aggregated NSW1 Regional Reference Price and Total Demand observations. See [AEMO Aggregated Price and Demand Data](https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/aggregated-data) and its [copyright permissions](https://www.aemo.com.au/privacy-and-legal-notices/copyright-permissions). The team's processed series covers November 2016 through October 2026. It averages complete sets of six five-minute observations after the 2021 market interval change. [The manifest](Data/prediction/processed/history_manifest.json) records acquisition timing, source files, hashes, interval rules and completeness. AEMO material retains its original copyright. Forecasts use a historical replay and stored training weights. The site, jobs, tariff and contract are declared simulation assumptions. Historical input vintages, live forecast accuracy, trading costs and long-run hedge effectiveness are outside the evaluation.

## Credits and version

JiuwenSwarm assisted with implementation, diagnosis and recorded execution. Codex assisted with packaging, model integration, verification and presentation. See [openJiuwen JiuwenSwarm](https://github.com/openJiuwen-ai/jiuwenswarm). Third-party libraries and data retain their own terms. See [LICENSE](LICENSE) for the project code. The course submission's community record supplies the exact final GitHub commit once published.
