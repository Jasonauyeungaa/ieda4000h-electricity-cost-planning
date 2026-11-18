# Electricity cost planning for flexible AI-computing work

Run one command per case. It builds the price forecast, runs a start-early baseline, runs a
linear-programming optimiser, verifies both plans with an independent checker, settles the
optional hedge, and writes every result to a timestamped folder under `Data/outputs/`.

The forecast is **built during the run** from the bundled model weights and the local price
history in `Data/prediction`. It is not downloaded. The run needs **no network access, no API
key and no manual step**.

## Setup

Use Python 3.12 or newer and keep the `Data` folder next to `Project`.

```sh
cd Project
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## One command per case

Run from `Project`.

**Case 1 — 22 October 2026, 25 GPU-hours, favourable hedge:**

```sh
.venv/bin/python run_case.py --day 2026-10-22 --scenario standard --hedge-strike 100
```

**Case 2 — 17 September 2026, 37.5 GPU-hours, adverse hedge:**

```sh
.venv/bin/python run_case.py --day 2026-09-17 --scenario more-work --hedge-strike 100
```

Each finishes in about 2–3 seconds on a laptop and prints a readable summary. It also writes a
timestamped folder, for example `Data/outputs/run_2026-10-22_standard_20261114T090812Z/`,
containing:

| file | contents |
|---|---|
| `forecast_<export_id>.json` | the `electricity.forecast.v2` export used for the run |
| `schedule_<run_id>.json` | full result: both plans, both checker reports, hedge settlement |
| `summary.json` | the compact figures below, machine readable |
| `summary.txt` | the same figures as terminal text |

`export_id` is derived from the generation timestamp, so the folder name and the
`forecast_<export_id>.json` / `schedule_<run_id>.json` filenames **change on every run**. The
prices and all costs are deterministic — a re-run reproduces the same numbers under a new
folder name. Cite the date and the price series, never the export id.

### Recorded fixtures

The two demo cases are frozen at fixed names. These are the reference artefacts:

| fixture folder | case |
|---|---|
| `Data/outputs/fixtures_20261114/2026-10-22_standard/` | 22 October 2026, 25 GPU-hours |
| `Data/outputs/fixtures_20261114/2026-09-17_more-work/` | 17 September 2026, 37.5 GPU-hours |

| case | forecast-priced saving | observed-priced saving |
|---|---|---|
| 2026-10-22, 25 GPU-h | A$0.1415 (0.2848% of bill) | A$0.2811 (0.5030% of bill) |
| 2026-09-17, 37.5 GPU-h | A$0.1946 (0.6012% of bill) | A$0.2267 (0.7037% of bill) |

Each fixture holds the forecast export, the full schedule result, `summary.json` and
`summary.txt`. Either can be replayed without rebuilding the forecast, using the
`forecast_<id>.json` actually present in the folder:

```sh
.venv/bin/python run_case.py --input outputs/fixtures_20261114/2026-10-22_standard/forecast_<id>.json --scenario standard --hedge-strike 100
```

### What the summary reports

Both price series are always reported, explicitly labelled, because they answer different
questions and are easy to confuse:

- **Forecast-priced** — the LP's own objective. This is the only fair measure of scheduling
  skill. Source field: `plans[i].forecast_cost`.
- **Observed-priced** — what the bill actually came to. This mixes scheduling skill with
  forecast error. Source field: `plans[i].actual_cost`.

Each case also reports GPU-hours delivered, the movable **energy** share and the movable
**cost** share, delta (the fractional cut in movable energy cost), checker verdicts, per-job
deadline windows, peak grid power against the 40 kW cap, hedge payoff and net cost.

**Energy share and cost share are different numbers and must not be conflated.** The base grid
load is 10 kW (8 kW IT × PUE 1.25), so most energy is immovable. On the 22 October case the
movable share of *energy* is 5.54%, but the movable share of the *bill* is 4.72% (start-early)
or 4.45% (optimised), because the tariff varies by interval and the optimiser moves work into
cheaper slots. Both appear in `summary.json` as `movable_energy_share_pct` and
`movable_cost.asap_share_pct` / `movable_cost.lp_share_pct`.

### Options

| flag | meaning |
|---|---|
| `--day YYYY-MM-DD` | build the forecast for this origin during the run |
| `--input imports/<file>.json` | replay a saved export instead of building one |
| `--scenario` | `standard` (25 GPU-h), `more-work` (37.5 GPU-h), `earlier` |
| `--tariff` | `linked` (wholesale-linked) or `flat` (constant A$0.30/kWh control) |
| `--model` | `lstm` (default), `fnn`, `cnn`, `prev-day`, `prev-week`, `auto` |
| `--hedge-strike AUD/MWh` | enable the hypothetical contract for difference |
| `--hedge-coverage` | fraction of base grid load covered (default 0.5) |

## Lower-level commands

`run_case.py` is the entry point. The individual stages remain available:

```sh
.venv/bin/python predict.py --day 2026-03-11 --model lstm
.venv/bin/python optimize.py --input imports/forecast_f8f0e81080c5ca76fa97f636.json --hedge-strike 100
.venv/bin/python -m unittest study.test_optimization
```

`predict.py` writes a new price export under `Data/imports` and a regional demand report under
`Data/outputs`, then prints both paths. To optimise from a newly generated forecast, pass the
printed `file` value to `optimize.py --input`. The scheduling model uses declared site and
workload assumptions. CVXPY builds the LP and HiGHS solves it. The checker uses the returned schedule and original inputs to recompute work, power and cost.

To import a different price forecast, put a versioned NSW1 `electricity.forecast.v2` file under
`Data/imports` and use its relative path. The forecast determines both plans before observed
prices are used for replay.

## Hedge example

The optional hedge settles a fixed volume against the difference between the NSW1 wholesale
price and an assumed strike. The 11 March example uses 50% of base grid load and A$100/MWh.
The workload and tariff are simulated. Fees, collateral, equity returns and actual traded
contracts are outside the example.

## Browser app and reuse

Open `START_WEBAPP.command` on macOS, or run `.venv/bin/python -m study.server` from
`Project` and visit http://127.0.0.1:8156/.

Use **1 · Run** to launch the forecast and solver with stages and logs, **2 · Raw data**
to inspect original AEMO records, **3 · Results** to see each job's run time and compare
both schedules, and **4 · Compare** to evaluate all five forecasting methods on the
same workload and observed prices. **Recorded examples** and **Offline replay** are
available without AEMO connectivity.

**Live AEMO** fetches the latest completed NSW1 DispatchIS interval and uses the
latest public AEMO Pre-dispatch report to make a separate plan. The app uses 8 to 24
hours of continuous published prices and a short-window demo workload. The three
default job windows are 0–4, 1–6 and 2–8 hours after the horizon starts. Earlier deadlines
subtract two hours from the ends of those windows. The latest observation and projected
prices are labelled separately and saved under `Data/outputs/live_market/`. The live
screen makes on-demand requests and leaves observed costs pending.

Rebuild the historical five-model comparison with
`.venv/bin/python -m study.evaluate_planning`. It uses the existing six test dates,
25 GPU-hours, and the linked tariff. All LP schedules are checked independently. Each
schedule is fixed before repricing at observed AEMO prices. Forecast MAE and replay savings
are separate metrics.

Energy share and cost share are labelled separately everywhere: `movable_energy_share_pct` for
energy claims, `movable_cost.asap_share_pct` and `movable_cost.lp_share_pct` for cost claims.

### Local API

Every POST is same-origin only: the server checks `Host`, `Origin` and the `X-Study: 1` header,
applies a CSP, and caps request bodies at 2 MB.

| method | endpoint | purpose |
|---|---|---|
| GET | `/api/reference` | recorded reference case and archive range |
| GET | `/api/fixtures` | both frozen demo cases with derived figures |
| GET | `/api/provenance?day=` | weight identity, import provenance, history rows |
| GET | `/api/replay?day=` | offline observed-price replay |
| GET | `/api/market?day=` | AEMO raw and half-hour rows |
| GET | `/api/imports` | valid forecast exports |
| GET | `/api/planning-evaluation` | stored six-date comparison |
| GET | `/api/live/dispatch` | latest available completed NSW1 observation |
| POST | `/api/live/plan` | AEMO Pre-dispatch forecast and checked schedule |
| POST | `/api/jobs` | start a run, returns a job id |
| GET | `/api/jobs?id=` | job status, elapsed seconds, result |
| POST | `/api/solve` | solve a saved export |
| POST | `/api/import` | import a forecast export |

Run exports and schedules are saved under `Data/outputs/workbench/`.
Choose **Saved forecast** to use or import an existing export.

**AEMO source data** shows the original settlement rows and the archived half-hour model
data for a chosen day. The server verifies the monthly CSV against the project manifest
before display. Download the chosen view as CSV. The original AEMO CSVs are located in
`Data/prediction/raw/aemo_nsw1_monthly.zip`. The browser inputs use historical archive data.
An execution today recomputes a historical case with stored model weights.

**Results** shows both plans, observed-price replay, independent job checks,
an optional fixed-volume contract, and a comparison using the same inputs under the other
tariff. See `skills/compare-ai-electricity-cost/SKILL.md` for a changed-workload example.

Interface references: [Grafana time-series panels](https://github.com/grafana/grafana/blob/main/docs/sources/visualizations/panels-visualizations/visualizations/time-series/index.md)
for timestamp-aligned traces and units, and [epftoolbox](https://github.com/jeslago/epftoolbox)
for same-target forecast comparison and baselines. The app runs the existing course project
models and does not import either repository's code.

## Limitations

The workload and tariffs are declared and synthetic. This is a planning experiment, not an
actual bill or a validated fuel-shock sensitivity. Forecast error is material: on spike days
the observed bill can be many times the forecast bill, and the scheduler's observed-priced
saving is then dominated by forecast error rather than scheduling skill. Report both series
and say which is which.

AEMO supplies the market observations. Earlier development used
[openJiuwen JiuwenSwarm](https://github.com/openJiuwen-ai/jiuwenswarm). Codex assisted with
packaging, model integration and verification. Third-party libraries retain their own licences.
