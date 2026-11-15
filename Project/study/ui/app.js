'use strict';

const $ = id => document.getElementById(id);
const modelNames = {
  lstm: 'LSTM', cnn: '1D CNN', fnn: 'FNN',
  'prev-day': 'Previous day', 'prev-week': 'Previous week', 'aemo-predispatch': 'AEMO Pre-dispatch',
};
const charts = {};
let current = null;
let market = null;
let activeView = 'forecast';
let fixtures = null;
let provenanceDay = null;
let replayLoaded = null;
let running = false;
let marketRequest = 0;

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const decimal = (value, digits = 2) => value == null ? 'Pending' : Number(value).toFixed(digits);
const aud = value => value == null ? 'Pending' : (value < 0 ? '−' : '') + 'A$' + decimal(Math.abs(value));
const time = stamp => stamp.slice(11, 16);
const tariffName = value => value === 'flat' ? 'Flat tariff' : 'Wholesale-linked';
const modelName = value => modelNames[value] || String(value).toUpperCase();
const runMarketDay = run => run.forecast.forecast_origin.slice(0, 10);

function status(id, message, error = false) {
  const node = $(id);
  node.textContent = message;
  node.toggleAttribute('data-error', error);
}

async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Study': '1'},
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}

function table(target, headings, rows, selected = -1) {
  $(target).innerHTML = '<div class="scroll" tabindex="0"><table><thead><tr>' +
    headings.map(h => '<th scope="col">' + escapeHtml(h) + '</th>').join('') +
    '</tr></thead><tbody>' + rows.map((row, index) =>
      '<tr' + (index === selected ? ' data-selected' : '') + '>' +
      row.map(value => '<td>' + escapeHtml(value) + '</td>').join('') + '</tr>').join('') +
    '</tbody></table></div>';
}

function draw(id, labels, datasets, unit, type = 'line') {
  charts[id]?.destroy();
  charts[id] = new Chart($(id), {
    type, data: {labels, datasets},
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {legend: {position: 'bottom', labels: {boxWidth: 24, padding: 17}}},
      scales: {
        x: {ticks: {maxTicksLimit: 9, maxRotation: 0}, grid: {display: false}},
        y: {title: {display: true, text: unit}, grid: {color: '#e3e9e2'}},
      },
    },
  });
}

function series(label, data, color, dashed = false) {
  return {label, data, borderColor: color, backgroundColor: color, borderWidth: 2.2,
    pointRadius: 0, pointHoverRadius: 4, borderDash: dashed ? [6, 4] : [], spanGaps: false};
}

function recordedMetric(forecast) {
  const actual = forecast.evaluation_actuals || [];
  const errors = forecast.slots.flatMap((slot, i) =>
    actual[i] == null ? [] : [slot.price - actual[i]]);
  const n = errors.length;
  return {
    method: forecast.model, n,
    mae: n ? errors.reduce((sum, error) => sum + Math.abs(error), 0) / n : null,
    rmse: n ? Math.sqrt(errors.reduce((sum, error) => sum + error ** 2, 0) / n) : null,
    bias: n ? errors.reduce((sum, error) => sum + error, 0) / n : null,
  };
}

function renderForecast() {
  const {run, comparison} = current;
  const forecast = run.forecast;
  const observed = forecast.evaluation_actuals || Array(forecast.slots.length).fill(null);
  const alternative = $('chartModel').value;
  const traces = [
    series('Observed NSW1', observed, '#203c36'),
    series(modelName(forecast.model) + ' · selected forecast', forecast.slots.map(slot => slot.price), '#278575', true),
  ];
  if (alternative && comparison?.predictions[alternative]) {
    traces.push(series(modelName(alternative), comparison.predictions[alternative], '#9c6031'));
  }
  const stamps = forecast.slots.map(slot => slot.target_start);
  const day = runMarketDay(run);
  const n = observed.filter(value => value != null).length;
  $('forecastMeta').textContent = `${day} · ${modelName(forecast.model)} · ${n}/${forecast.slots.length} observed intervals · NEM time`;
  draw('priceCanvas', stamps.map(time), traces, 'A$/MWh');
  table('priceRows', ['Interval start (NEM)', 'Forecast (A$/MWh)', 'Observed (A$/MWh)'],
    stamps.map((stamp, i) => [stamp, decimal(forecast.slots[i].price), decimal(observed[i])]));
  const metrics = comparison?.metrics || [recordedMetric(forecast)];
  $('metricNote').textContent = comparison
    ? 'MAE and RMSE use the same observed intervals for each model. Lower is better.'
    : current.live
      ? 'This is an AEMO forecast for future intervals. Accuracy can be measured after observations arrive.'
      : 'This export contains one model. Select Run model to compare all five methods.';
  table('metrics', ['Model', 'MAE (A$/MWh)', 'RMSE (A$/MWh)', 'Bias (A$/MWh)', 'Intervals scored'],
    metrics.map(row => [modelName(row.method), decimal(row.mae), decimal(row.rmse), decimal(row.bias), row.n]),
    metrics.findIndex(row => row.method === forecast.model));
}

function jobBlocks(plan, job) {
  const blocks = [];
  for (const slot of plan.plan.schedule) {
    const count = slot.allocations_gpu[job] || 0;
    if (count < 0.00001) continue;
    const last = blocks.at(-1);
    if (last?.end === slot.start_iso && Math.abs(last.count - count) < 0.00001) last.end = slot.end_iso;
    else blocks.push({start: slot.start_iso, end: slot.end_iso, count});
  }
  return blocks.map(({start, end, count}) => {
    const startLabel = current.live ? start.slice(5, 16).replace('T', ' ') : time(start);
    const endLabel = start.slice(0, 10) === end.slice(0, 10) ? time(end)
      : current.live ? end.slice(5, 16).replace('T', ' ') : '24:00';
    return `${startLabel}–${endLabel} (${Number(count.toFixed(2))} GPUs)`;
  }).join(' · ');
}

function renderDecisions() {
  const outcomes = current?.comparison?.outcomes;
  if (!outcomes) {
    $('decisionRows').replaceChildren();
    $('decisionMeta').textContent = 'Run a historical case to compare all five forecasting methods on one workload and tariff.';
    return;
  }
  const run = current.run;
  $('decisionMeta').textContent = `${runMarketDay(run)} · ${tariffName(run.tariff)} · same workload and observed prices. Forecast budgets use each model’s own prices.`;
  table('decisionRows', ['Forecast used for LP', 'Price MAE (A$/MWh)', 'Forecast budget', 'LP replay cost', 'Replay saving vs start early', 'Check'],
    outcomes.map(row => [modelName(row.method), decimal(row.mae), aud(row.budget), aud(row.replay), aud(row.saving), row.check]),
    outcomes.findIndex(row => row.method === run.forecast.model));
}

function renderSchedule() {
  const {run, recorded, alternativeTariff} = current;
  const [early, lp] = run.plans;
  const dates = early.plan.schedule.map(slot => time(slot.start_iso));
  $('scheduleMeta').textContent = `${runMarketDay(run)} · ${modelName(run.forecast.model)} · ${tariffName(run.tariff)} · ${recorded ? 'Recorded result' : run.run_id}`;
  const saving = run.actual_saving;
  $('planGuide').textContent = current.live
    ? `AEMO forecast covers ${run.forecast.slots.length / 2} hours. Live demo job windows use offsets of 0–4, 1–6 and 2–8 hours, with earlier deadlines two hours shorter. Observed cost is pending.`
    : run.tariff === 'flat'
    ? 'Every half-hour has the same tariff. Both schedules use the same energy, so starting early is the simpler choice.'
    : saving == null
      ? 'This plan uses forecast prices. Its observed cost is pending.'
      : saving < -0.005
        ? `In this replay, starting early cost ${aud(-saving)} less. The forecast-based schedule had expected a saving of ${aud(run.predicted_saving)}.`
        : `In this replay, the LP schedule reduced cost by ${aud(Math.max(0, saving))}.`;
  table('jobTimes', ['Job', 'Work required', 'Start early (NEM)', 'LP schedule (NEM)'],
    Object.entries(lp.check.per_job).map(([job, info]) =>
      [job, decimal(info.required_gpu_hours, 1) + ' GPU-h', jobBlocks(early, job), jobBlocks(lp, job)]),
  );
  table('costRows', ['Plan', 'Forecast budget', 'Observed-price replay', 'GPU-hours', 'Grid energy (kWh)', 'Check'],
    [...run.plans.map(plan => [
      plan === early ? 'Start early' : 'LP schedule', aud(plan.forecast_cost), aud(plan.actual_cost), decimal(plan.check.summary.total_gpu_hours),
      decimal(plan.check.summary.total_grid_energy_kwh), plan.check.verdict,
    ]), ['Difference (start early − LP)', aud(run.predicted_saving), aud(run.actual_saving), '', '', '']], 1);
  if (alternativeTariff) {
    table('tariffRows', ['Same day and workload', 'Forecast cost difference', 'Replay cost difference'],
      [run, alternativeTariff].map(item => [tariffName(item.tariff), aud(item.predicted_saving), aud(item.actual_saving)]));
  } else {
    $('tariffRows').replaceChildren();
  }
  draw('scheduleCanvas', dates, [
    {label: 'Start early', data: early.plan.schedule.map(row => row.total_allocation_gpu), backgroundColor: '#9cae96'},
    {label: 'LP schedule', data: lp.plan.schedule.map(row => row.total_allocation_gpu), backgroundColor: '#237568'},
  ], 'GPUs', 'bar');
  table('checks', ['LP job', 'Required GPU-hours', 'Delivered GPU-hours', 'Allowed window (hours)'],
    Object.entries(lp.check.per_job).map(([job, info]) =>
      [job, decimal(info.required_gpu_hours), decimal(info.done_gpu_hours), decimal(info.window_hours)]));
  table('allocationRows', ['Interval start (NEM)', 'Start early GPUs', 'LP GPUs', 'LP grid kW'],
    lp.plan.schedule.map((slot, i) =>
      [slot.start_iso, decimal(early.plan.schedule[i].total_allocation_gpu), decimal(slot.total_allocation_gpu), decimal(slot.grid_power_kw)]));
  const hedge = run.hedge;
  $('hedgePanel').hidden = !hedge;
  if (hedge) {
    $('hedgeMeta').textContent = `${decimal(hedge.hedged_mwh, 3)} MWh over the horizon · ${aud(hedge.strike_aud_mwh)}/MWh strike. A positive payoff lowers net cost.`;
    table('hedgeRows', ['Price case', 'LP cost', 'CfD payoff', 'LP + CfD'],
      hedge.scenarios.map(row => [row.scenario, aud(row.schedule_aud), aud(row.hedge_payoff_aud), aud(row.schedule_and_hedge_aud)]));
  }
}

function switchView(view) {
  activeView = view;
  document.querySelectorAll('[data-view]').forEach(button => {
    const selected = button.dataset.view === view;
    button.setAttribute('aria-pressed', selected);
    $(button.dataset.view).hidden = !selected;
  });
  if (current && view === 'forecast') renderForecast();
  if (current && view === 'schedule') renderSchedule();
  if (view === 'results') renderResults();
  if (view === 'compare') { renderDecisions(); renderCompare(); renderFreshCompare(); loadEvaluation(); }
  if (view === 'data') {
    if (market?.day !== $('dataDay').value) loadMarket();
    if (provenanceDay !== $('dataDay').value) loadProvenance($('dataDay').value);
  }
  if (view === 'replay') {
    if (replayLoaded !== $('replayDay').value) loadReplay($('replayDay').value);
  }
}

function showRun(result) {
  current = result;
  const {run, comparison, recorded} = result;
  const lp = run.plans[1];
  $('budget').textContent = aud(lp.forecast_cost);
  $('replayCost').textContent = aud(lp.actual_cost);
  const saving = run.actual_saving;
  $('difference').textContent = saving == null ? 'Pending' : aud(Math.abs(saving));
  $('differenceDetail').textContent = saving == null ? 'Waiting for observed prices'
    : Math.abs(saving) < 0.005 ? 'Same cost in replay' : saving < 0 ? 'More expensive in replay' : 'Cheaper in replay';
  const passed = run.plans.filter(plan => plan.check.verdict === 'PASS').length;
  $('verify').textContent = `${passed} / ${run.plans.length} pass`;
  const peak = Math.max(...lp.plan.schedule.map(slot => slot.grid_power_kw));
  $('verifyDetail').textContent = `${decimal(peak, 1)} kW LP peak · 40 kW limit`;
  $('chartModelLabel').hidden = !comparison;
  $('chartModel').replaceChildren(new Option('Selected and observed', ''));
  Object.keys(comparison?.predictions || {}).filter(model => model !== run.forecast.model)
    .forEach(model => $('chartModel').add(new Option(modelName(model), model)));
  if (!result.live) $('dataDay').value = runMarketDay(run);
  $('compareTariff').textContent = run.tariff === 'flat' ? 'Compare linked tariff' : 'Compare flat tariff';
  $('downloadRun').disabled = false;
  status('status', `${recorded ? 'Recorded example' : 'Run complete'} · ${runMarketDay(run)} · ${modelName(run.forecast.model)} · ${tariffName(run.tariff)}`);
  switchView(activeView);
  renderFreshCompare();
}

function controls() {
  const tariff = $('tariff').value;
  return {
    tariff, scenario: $('scenario').value,
    ...(tariff === 'linked' && $('includeHedge').checked
      ? {hedge_strike: $('strike').valueAsNumber, hedge_coverage: $('coverage').valueAsNumber / 100}
      : {}),
  };
}

function syncControls() {
  $('runButton').disabled = running;
  $('runSaved').disabled = running || !$('savedForecast').value;
  $('compareTariff').disabled = running || !current;
  $('planLive').disabled = running;
  const linked = $('tariff').value === 'linked';
  $('includeHedge').disabled = !linked;
  $('strike').disabled = $('coverage').disabled = !linked || !$('includeHedge').checked;
}

async function execute(path, payload) {
  if (running) return;
  running = true;
  syncControls();
  $('jobMeter').hidden = true;
  status('status', 'Running Python and checking both schedules…');
  try {
    const response = await api(path, payload);
    showRun(path === '/api/solve' ? {run: response, comparison: null, recorded: false} : response);
  } catch (error) {
    status('status', `Run failed. ${error.message}`, true);
  } finally {
    running = false;
    syncControls();
  }
}


// ---------------------------------------------------------------------------
// Frozen fixtures: both demo cases, always shown together.
// ---------------------------------------------------------------------------

async function loadFixtures(quiet = false) {
  if (!quiet) status('resultsStatus', 'Reading frozen fixtures…');
  try {
    fixtures = await api('/api/fixtures');
    renderResults();
    renderCompare();
    renderFreshCompare();
    status('resultsStatus', `${fixtures.cases.length} frozen cases read from ${fixtures.folder}/ by folder and glob.`);
  } catch (error) {
    status('resultsStatus', error.message, true);
  }
}

function caseCard(c) {
  const fav = c.hedge && c.hedge.observed_payoff_aud != null && c.hedge.observed_payoff_aud >= 0;
  return '<article class="case-card ' + (fav ? 'fav' : 'adv') + '">' +
    '<header><h3>' + escapeHtml(c.day) + '</h3><span class="pill">' +
    (fav ? 'hedge pays' : 'hedge costs') + '</span></header>' +
    '<p class="small">' + escapeHtml(c.scenario) + ' · ' + decimal(c.gpu_hours, 1) +
    ' GPU-h · ' + escapeHtml(tariffName(c.tariff)) + '</p>' +
    '<dl class="case-facts">' +
    '<div><dt>ASAP · forecast prices</dt><dd>' + aud(c.forecast_priced.asap_aud) + '</dd></div>' +
    '<div><dt>LP · forecast prices</dt><dd>' + aud(c.forecast_priced.lp_aud) + '</dd></div>' +
    '<div><dt>Saving · forecast prices</dt><dd>' + aud(c.forecast_priced.saving_aud) +
    ' (' + decimal(c.forecast_priced.saving_pct, 4) + '%)</dd></div>' +
    '<div><dt>ASAP · observed prices</dt><dd>' + aud(c.observed_priced.asap_aud) + '</dd></div>' +
    '<div><dt>LP · observed prices</dt><dd>' + aud(c.observed_priced.lp_aud) + '</dd></div>' +
    '<div><dt>Saving · observed prices</dt><dd>' + aud(c.observed_priced.saving_aud) +
    ' (' + decimal(c.observed_priced.saving_pct, 4) + '%)</dd></div>' +
    '<div><dt>Movable energy share</dt><dd>' + decimal(c.movable_energy_share_pct, 2) + '% of energy</dd></div>' +
    '<div><dt>Movable cost share</dt><dd>' + decimal(c.movable_cost.asap_share_pct, 2) +
    '% start-early · ' + decimal(c.movable_cost.lp_share_pct, 2) + '% optimised</dd></div>' +
    '<div><dt>Checker verdicts</dt><dd>' + escapeHtml(c.checks.asap) + ' / ' + escapeHtml(c.checks.lp) + '</dd></div>' +
    '<div><dt>Hedge payoff · observed</dt><dd>' + aud(c.hedge?.observed_payoff_aud) + '</dd></div>' +
    '<div><dt>Net cost · observed</dt><dd>' + aud(c.hedge?.observed_net_aud) + '</dd></div>' +
    '</dl></article>';
}

function renderResults() {
  if (!fixtures) return;
  $('resultCards').innerHTML = fixtures.cases.map(caseCard).join('');
}

let evaluationLoaded = false;
async function loadEvaluation() {
  if (evaluationLoaded) return;
  evaluationLoaded = true;
  try {
    const data = await api('/api/planning-evaluation');
    table('multiDayRows', ['Forecast used for LP', 'Mean price MAE', 'Mean replay saving', 'Median replay saving', 'Days cheaper', 'Days more expensive'],
      data.methods.map(row => [modelName(row.model), decimal(row.mean_mae) + ' A$/MWh', aud(row.mean_saving),
        aud(row.median_saving), row.cheaper + ' / ' + data.dates.length, row.more_expensive + ' / ' + data.dates.length]));
  } catch (error) {
    $('multiDayRows').textContent = error.message;
    evaluationLoaded = false;
  }
}

function renderCompare() {
  if (!fixtures || fixtures.cases.length < 2) return;
  const [a, b] = fixtures.cases;
  draw('compareCanvas', ['Saving · forecast', 'Saving · observed', 'Hedge payoff · observed'], [
    {label: a.day + ' (favourable)', backgroundColor: '#237568',
     data: [a.forecast_priced.saving_aud, a.observed_priced.saving_aud, a.hedge?.observed_payoff_aud]},
    {label: b.day + ' (adverse)', backgroundColor: '#9c6031',
     data: [b.forecast_priced.saving_aud, b.observed_priced.saving_aud, b.hedge?.observed_payoff_aud]},
  ], 'A$', 'bar');
  const pct = (x) => decimal(x, 4) + '%';
  table('compareTable', ['Measure', a.day + ' · ' + a.scenario, b.day + ' · ' + b.scenario], [
    ['GPU-hours delivered', decimal(a.gpu_hours, 1), decimal(b.gpu_hours, 1)],
    ['ASAP cost · forecast prices', aud(a.forecast_priced.asap_aud), aud(b.forecast_priced.asap_aud)],
    ['LP cost · forecast prices', aud(a.forecast_priced.lp_aud), aud(b.forecast_priced.lp_aud)],
    ['Saving · forecast prices', aud(a.forecast_priced.saving_aud) + ' (' + pct(a.forecast_priced.saving_pct) + ')',
     aud(b.forecast_priced.saving_aud) + ' (' + pct(b.forecast_priced.saving_pct) + ')'],
    ['ASAP cost · observed prices', aud(a.observed_priced.asap_aud), aud(b.observed_priced.asap_aud)],
    ['LP cost · observed prices', aud(a.observed_priced.lp_aud), aud(b.observed_priced.lp_aud)],
    ['Saving · observed prices', aud(a.observed_priced.saving_aud) + ' (' + pct(a.observed_priced.saving_pct) + ')',
     aud(b.observed_priced.saving_aud) + ' (' + pct(b.observed_priced.saving_pct) + ')'],
    ['Movable ENERGY share', decimal(a.movable_energy_share_pct, 2) + '% of energy', decimal(b.movable_energy_share_pct, 2) + '% of energy'],
    ['Movable COST share · start-early', decimal(a.movable_cost.asap_share_pct, 2) + '% of bill', decimal(b.movable_cost.asap_share_pct, 2) + '% of bill'],
    ['Movable COST share · optimised', decimal(a.movable_cost.lp_share_pct, 2) + '% of bill', decimal(b.movable_cost.lp_share_pct, 2) + '% of bill'],
    ['Checker verdicts', a.checks.asap + ' / ' + a.checks.lp, b.checks.asap + ' / ' + b.checks.lp],
    ['Hedge payoff · forecast', aud(a.hedge?.forecast_payoff_aud), aud(b.hedge?.forecast_payoff_aud)],
    ['Hedge payoff · observed', aud(a.hedge?.observed_payoff_aud), aud(b.hedge?.observed_payoff_aud)],
    ['Net cost · observed', aud(a.hedge?.observed_net_aud), aud(b.hedge?.observed_net_aud)],
  ]);
}

function renderFreshCompare() {
  const panel = $('freshCompare');
  if (!panel) return;
  if (!current || current.recorded || !fixtures) { panel.hidden = true; hideRepro(); return; }
  const run = current.run;
  const day = runMarketDay(run);
  const fixture = run.tariff === 'linked' && run.forecast.model === 'lstm' &&
    fixtures.cases.find(c => c.day === day && c.scenario === run.scenario);
  if (!fixture) { panel.hidden = true; hideRepro(); return; }
  const hedgeRow = (run.hedge?.scenarios || []).find(row => row.scenario === 'Observed replay');
  const pairs = [
    ['ASAP · forecast prices', run.plans[0].forecast_cost, fixture.forecast_priced.asap_aud],
    ['LP · forecast prices', run.plans[1].forecast_cost, fixture.forecast_priced.lp_aud],
    ['Saving · forecast prices', run.predicted_saving, fixture.forecast_priced.saving_aud],
    ['ASAP · observed prices', run.plans[0].actual_cost, fixture.observed_priced.asap_aud],
    ['LP · observed prices', run.plans[1].actual_cost, fixture.observed_priced.lp_aud],
    ['Saving · observed prices', run.actual_saving, fixture.observed_priced.saving_aud],
    ['Hedge payoff · observed', hedgeRow?.hedge_payoff_aud, fixture.hedge?.observed_payoff_aud],
  ];
  panel.hidden = false;
  table('freshTable', ['Measure', 'Fresh run', 'Frozen fixture', 'Difference'],
    pairs.map(([label, fresh, frozen]) => [label, aud(fresh), aud(frozen),
      fresh == null || frozen == null ? 'Pending' : aud(fresh - frozen)]));
  $('freshMeta').textContent = 'Fresh run ' + run.run_id + ' against the ' + day + ' / ' +
    run.scenario + ' fixture. Costs are deterministic, so every difference should round to zero. ' +
    'The export_id differs by design because it is timestamp-derived.';
  // Surface the verdict where the user already is, so the strongest evidence in the app
  // does not require knowing to open Compare.
  const repro = $('reproLine');
  if (repro) {
    const worst = Math.max(...pairs.map(([, fresh, frozen]) =>
      fresh == null || frozen == null ? 0 : Math.abs(fresh - frozen)));
    const exact = worst < 0.005;
    $('reproVerdict').textContent = exact
      ? 'Reproduces the frozen fixture exactly \u2014 all ' + pairs.length + ' measures differ by A$0.00.'
      : 'Differs from the frozen fixture by up to A$' + worst.toFixed(4) + '.';
    repro.hidden = false;
    repro.dataset.match = exact ? 'yes' : 'no';
  }
}

function hideRepro() {
  const repro = $('reproLine');
  if (repro) { repro.hidden = true; delete repro.dataset.match; }
}

// ---------------------------------------------------------------------------
// Raw inputs: weight identity, import provenance, history rows.
// ---------------------------------------------------------------------------

async function loadProvenance(day) {
  status('weightsMeta', 'Reading weight identity…');
  try {
    const data = await api('/api/provenance?day=' + encodeURIComponent(day));
    provenanceDay = day;
    $('weightsMeta').textContent = data.weights.files.length
      ? data.weights.files.length + ' weight files under ' + data.weights.folder + '/ · run ' + data.weights.run
      : 'No weight files resolved for this day.';
    table('weightsRows', ['Weight file', 'Bytes', 'SHA-256'],
      data.weights.files.map(f => [f.name, String(f.bytes), f.sha256.slice(0, 40) + '…']));
    if (data.import) {
      $('importMeta').textContent = data.import.file + ' · ' + data.import.schema;
      table('importRows', ['Field', 'Value'], [
        ['export_id', data.import.export_id],
        ['model / version', data.import.model + ' · ' + data.import.model_version],
        ['kind', data.import.kind],
        ['generated_at', data.import.generated_at],
        ['input_hash', data.import.input_hash || '—'],
        ['weight_sha256', (data.import.weight_sha256 || '—').slice(0, 40) + '…'],
      ]);
    } else {
      $('importMeta').textContent = 'No frozen fixture for this day, so no import provenance is shown.';
      $('importRows').replaceChildren();
    }
    $('historyMeta').textContent = data.history.rows_for_day + ' rows for ' + data.day + ' · ' +
      data.history.first + ' → ' + data.history.last + ' · ' + data.history.total_rows +
      ' rows in ' + data.history.file;
    table('historyRows', ['Timestamp', 'Price (A$/MWh)', 'Demand (MW)', 'Source interval'],
      data.history.sample.map(r => [r.timestamp, decimal(r.price), decimal(r.demand), r.source_minutes + ' min']));
  } catch (error) {
    status('weightsMeta', error.message, true);
  }
}

// ---------------------------------------------------------------------------
// Offline replay of observed prices. No live feed is contacted.
// ---------------------------------------------------------------------------

async function loadReplay(day) {
  status('replayStatus', 'Reading observed prices…');
  try {
    const data = await api('/api/replay?day=' + encodeURIComponent(day));
    replayLoaded = day;
    $('liveFlag').textContent = data.live_feed;
    $('replaySource').textContent = data.source;
    $('replayCount').textContent = data.count;
    status('replayStatus', data.day + ' · ' + data.count + ' observed half-hours · mean A$' +
      decimal(data.stats.mean) + '/MWh · max A$' + decimal(data.stats.max) + '/MWh · ' +
      data.stats.over_1000 + ' intervals above A$1,000');
    draw('replayCanvas', data.stamps.map(s => s.slice(11, 16)),
      [series('Observed NSW1', data.actuals, '#203c36')], 'A$/MWh');
    table('replayStats', ['Statistic', 'Value'], [
      ['Intervals', data.count],
      ['Minimum', aud(data.stats.min) + '/MWh'],
      ['Mean', aud(data.stats.mean) + '/MWh'],
      ['Maximum', aud(data.stats.max) + '/MWh'],
      ['Intervals above A$1,000', data.stats.over_1000],
    ]);
    table('replayRows', ['Timestamp (NEM)', 'Observed (A$/MWh)'],
      data.stamps.map((s, i) => [s, decimal(data.actuals[i])]));
  } catch (error) {
    status('replayStatus', error.message, true);
  }
}

// ---------------------------------------------------------------------------
// Run the real model as a background job, with status and elapsed time.
// ---------------------------------------------------------------------------

async function runJob(payload) {
  if (running) return;
  running = true;
  syncControls();
  const started = performance.now();
  $('jobMeter').hidden = false;
  $('jobState').textContent = 'queued';
  $('jobStage').textContent = 'queued';
  $('jobElapsed').textContent = '0.0s';
  $('jobExit').textContent = '—';
  $('jobPath').textContent = '';
  $('stageLog').textContent = '';
  status('status', 'Queued. Building the forecast, solving and checking both schedules…');
  try {
    const {job_id} = await api('/api/jobs', payload);
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 350));
      const job = await api('/api/jobs?id=' + encodeURIComponent(job_id));
      $('jobState').textContent = job.status;
      $('jobStage').textContent = job.stage || '—';
      $('jobElapsed').textContent = job.elapsed_s.toFixed(2) + 's';
      $('jobExit').textContent = job.exit_code == null ? '—' : String(job.exit_code);
      if (job.log_path) $('jobPath').textContent = 'log: ' + job.log_path + ' · ' + (job.command || '');
      if (job.log_tail && job.log_tail.length) {
        $('stageLog').textContent = job.log_tail.join('\n');
        $('stageLog').scrollTop = $('stageLog').scrollHeight;
      }
      if (job.status === 'done') {
        showRun(job.result);
        status('status', 'Run complete · ' + runMarketDay(job.result.run) + ' · ' +
          ((performance.now() - started) / 1000).toFixed(2) + 's elapsed · job ' + job_id);
        break;
      }
      if (job.status === 'error') throw new Error(job.error || 'Job failed.');
      if (job.elapsed_s > 180) throw new Error('Run timed out after 180s.');
    }
  } catch (error) {
    status('status', 'Run failed. ' + error.message, true);
    $('jobState').textContent = 'error';
  } finally {
    running = false;
    syncControls();
  }
}

function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function marketRows() {
  if (!market) return;
  const raw = $('dataFormat').value === 'raw';
  const columns = raw ? ['REGION', 'SETTLEMENTDATE', 'TOTALDEMAND', 'RRP', 'PERIODTYPE']
    : ['timestamp', 'price', 'demand', 'source_minutes'];
  return {
    columns, rows: (raw ? market.raw : market.processed).map(row => columns.map(column => row[column])),
    titles: raw ? ['Region', 'Settlement time (NEM)', 'Demand (MW)', 'RRP (A$/MWh)', 'Period type']
      : ['Half-hour start (NEM)', 'Price (A$/MWh)', 'Demand (MW)', 'Source interval (min)'],
  };
}

function renderMarket() {
  const data = marketRows();
  if (!data) return;
  table('dataRows', data.titles, data.rows);
  const raw = $('dataFormat').value === 'raw';
  status('dataStatus', raw ? `${data.rows.length} source rows · ${market.source_minutes.join(' / ')}-minute settlement intervals`
    : `${data.rows.length} half-hours · prices and demand as used by the historical model`);
  $('sourceMeta').textContent = `${market.file} · SHA-256 verified against the project manifest: ${market.sha256}`;
  $('downloadData').disabled = false;
}

async function loadMarket() {
  const version = ++marketRequest;
  status('dataStatus', 'Reading AEMO source rows…');
  $('downloadData').disabled = true;
  $('dataRows').replaceChildren();
  $('sourceMeta').textContent = '';
  market = null;
  try {
    const result = await api('/api/market?day=' + encodeURIComponent($('dataDay').value));
    if (version !== marketRequest) return;
    market = result;
    renderMarket();
  } catch (error) {
    if (version === marketRequest) status('dataStatus', error.message, true);
  }
}

async function loadSaved(selection) {
  try {
    const {files, errors} = await api('/api/imports');
    $('savedForecast').replaceChildren(...files.map(file => new Option(
      `${file.origin.slice(0, 10)} · ${modelName(file.model)} · ${file.export_id.slice(0, 6)}`, file.file)));
    if (selection) $('savedForecast').value = selection;
    $('savedStatus').textContent = `${files.length} valid exports${errors.length ? ` · ${errors.length} rejected` : ''}`;
  } catch (error) {
    status('savedStatus', error.message, true);
  } finally {
    syncControls();
  }
}

$('runForm').addEventListener('submit', event => {
  event.preventDefault();
  runJob({day: $('day').value, model: $('model').value, ...controls()});
});
$('runSaved').addEventListener('click', () => {
  if ($('runForm').reportValidity()) execute('/api/solve', {file: $('savedForecast').value, ...controls()});
});
$('tariff').addEventListener('change', syncControls);
$('includeHedge').addEventListener('change', syncControls);
$('savedForecast').addEventListener('change', syncControls);
$('savedOptions').addEventListener('toggle', () => {
  if ($('savedOptions').open && !$('savedForecast').options.length) loadSaved();
});
$('upload').addEventListener('change', async () => {
  const file = $('upload').files[0];
  if (!file) return;
  try {
    if (file.size > 2_000_000) throw Error('Export exceeds 2 MB.');
    const imported = await api('/api/import', JSON.parse(await file.text()));
    await loadSaved(imported.file);
  } catch (error) {
    status('savedStatus', `Import rejected. ${error.message}`, true);
  } finally {
    $('upload').value = '';
  }
});
document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => {
  const day = button.dataset.preset;
  $('day').value = day;
  $('dataDay').value = day;
  $('scenario').value = day === '2026-10-22' ? 'standard' : 'more-work';
  $('model').value = 'lstm';
  $('tariff').value = 'linked';
  $('includeHedge').checked = true;
  $('strike').value = 100;
  $('coverage').value = 50;
  syncControls();
  if (activeView === 'data') loadMarket();
  status('status', `Preset selected · ${day}. Select Run model to compute the case.`);
}));
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
$('reproLink').addEventListener('click', () => switchView('compare'));

// Keyboard shortcuts: 1-N switch views, r runs the model, Escape returns to Results.
// Guard: never fire while focus is inside a text field, or editing a day or a filename
// would switch views underneath the user. Escape is handled BEFORE that guard so it can
// always get the user out, and modifier chords are left to the browser.
document.addEventListener('keydown', event => {
  const tag = (event.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'select' || tag === 'textarea'
    || event.target.isContentEditable === true;
  if (event.key === 'Escape') { switchView('results'); return; }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === 'r' || event.key === 'R') {
    event.preventDefault();
    if (!running) $('runForm').requestSubmit();
    return;
  }
  const views = Array.from(document.querySelectorAll('[data-view]')).map(b => b.dataset.view);
  const index = Number(event.key);
  if (Number.isInteger(index) && index >= 1 && index <= views.length) {
    event.preventDefault();
    switchView(views[index - 1]);
  }
});
$('chartModel').addEventListener('change', renderForecast);
$('dataForm').addEventListener('submit', event => { event.preventDefault(); loadMarket(); });
$('dataFormat').addEventListener('change', renderMarket);
$('reloadFixtures').addEventListener('click', () => loadFixtures());
$('replayForm').addEventListener('submit', event => { event.preventDefault(); loadReplay($('replayDay').value); });
$('downloadData').addEventListener('click', () => {
  const data = marketRows();
  if (!data) return;
  const csv = [data.columns, ...data.rows].map(row =>
    row.map(value => '"' + String(value ?? '').replaceAll('"', '""') + '"').join(',')).join('\n');
  download(`NSW1_${market.day}_${$('dataFormat').value}.csv`, csv + '\n', 'text/csv');
});
$('downloadRun').addEventListener('click', () => {
  if (current) download(`${current.run.run_id}.json`, JSON.stringify(current, null, 2), 'application/json');
});
$('compareTariff').addEventListener('click', async () => {
  if (!current || running) return;
  running = true;
  syncControls();
  const {run, input_file} = current;
  const tariff = run.tariff === 'flat' ? 'linked' : 'flat';
  status('status', `Checking the same case with the ${tariffName(tariff).toLowerCase()}…`);
  try {
    current.alternativeTariff = await api('/api/solve', {
      file: input_file || run.input_file, scenario: run.scenario, tariff,
    });
    renderSchedule();
    status('status', 'Tariff comparison complete · same forecast and GPU workload.');
  } catch (error) {
    status('status', error.message, true);
  } finally {
    running = false;
    syncControls();
  }
});

$('refreshLive').addEventListener('click', async () => {
  $('refreshLive').disabled = true;
  $('liveSource').hidden = true;
  $('liveRows').replaceChildren();
  status('liveStatus', 'Fetching the latest AEMO DispatchIS report…');
  try {
    const report = await api('/api/live/dispatch');
    status('liveStatus', `${report.fresh ? 'Recent observation' : 'Stale observation'} · ${report.age_minutes} minutes old at retrieval`);
    table('liveRows', ['Interval end (NEM)', 'Observed NSW1 price', 'Demand (MW)', 'Checked at (NEM)'],
      [[report.interval_end, aud(report.price) + '/MWh', decimal(report.demand, 1), report.checked_at]]);
    $('liveSource').href = report.url;
    $('liveSource').hidden = false;
  } catch (error) {
    status('liveStatus', error.message, true);
  } finally {
    $('refreshLive').disabled = false;
  }
});

$('planLive').addEventListener('click', async () => {
  if (running || !$('runForm').reportValidity()) return;
  running = true;
  syncControls();
  status('liveStatus', 'Fetching AEMO Pre-dispatch and computing both schedules…');
  try {
    const result = await api('/api/live/plan', controls());
    $('jobMeter').hidden = true;
    showRun(result);
    const forecast = result.run.forecast;
    status('liveStatus', `Plan computed. NEM horizon ${forecast.forecast_origin} to ${forecast.slots.at(-1).target_end}. See Schedule & costs.`);
    $('liveSource').href = forecast.source_url;
    $('liveSource').hidden = false;
    switchView('schedule');
  } catch (error) {
    status('liveStatus', error.message, true);
  } finally {
    running = false;
    syncControls();
  }
});

async function init() {
  try {
    const result = await api('/api/reference');
    const {first, last, months} = result.range;
    $('day').max = last;
    $('dataDay').min = first;
    $('dataDay').max = last;
    $('replayDay').min = first;
    $('replayDay').max = last;
    $('archiveRange').textContent = `${first.slice(0, 4)}–${last.slice(0, 4)} · ${months} months`;
    showRun(result);
    await loadFixtures(true);
  } catch (error) {
    status('status', error.message, true);
  } finally {
    syncControls();
  }
}
init();
