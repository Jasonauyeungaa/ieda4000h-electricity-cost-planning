'use strict';const $=id=>document.getElementById(id);let selected='',lastResult=null,comparedResult=null,solving=false;const charts={};
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=(x,digits=2)=>x==null?'Pending':Number(x).toFixed(digits);
const table=(h,r)=>'<div class="scroll"><table><thead><tr>'+h.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+r.map(a=>'<tr>'+a.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
async function api(p,d){const r=await fetch(p,d===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Study':'1'},body:JSON.stringify(d)});const j=await r.json();if(!r.ok)throw Error(j.error||'Request failed');return j;}
function chart(id,type,labels,datasets){if(charts[id])charts[id].destroy();charts[id]=new Chart($(id),{type,data:{labels,datasets},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{position:'bottom'}},scales:{x:{ticks:{maxTicksLimit:10,maxRotation:0}}}}});}
async function preview(){if(!selected)return;const d=await api('/api/preview?file='+encodeURIComponent(selected)),f=d.forecast;$('provenance').textContent=`${f.model.toUpperCase()} · ${f.model_version} · ${f.kind}. Origin ${f.forecast_origin}. Executed ${f.generated_at}. File SHA256 ${d.sha256}.`;$('sample').innerHTML=table(['Target start · NEM','Target end','Price AUD/MWh'],f.slots.slice(0,6).map(x=>[x.target_start,x.target_end,fmt(x.price)]));chart('priceChart','line',f.slots.map(x=>x.target_start.slice(5,16).replace('T',' ')),[{label:'Forecast AUD/MWh',data:f.slots.map(x=>x.price),borderColor:'#815436',pointRadius:0},{label:'Observed AUD/MWh',data:f.evaluation_actuals || [],borderColor:'#2e625d',pointRadius:0}]);}
async function scan(updateStatus = true){try{const d=await api('/api/imports');document.querySelector('nav a[href="/slides/Presentation.html"]').hidden=!d.presentation_available;const before=selected;if(!d.files.some(x=>x.file===selected))selected=(d.files.find(file=>file.file==='imports/forecast_f8f0e81080c5ca76fa97f636.json')||d.files[0])?.file||'';$('files').innerHTML=d.files.map(x=>`<option value="${esc(x.file)}">${esc(x.origin.slice(0,10)+' · '+x.model.toUpperCase()+' · '+x.export_id.slice(0,6))}</option>`).join('');$('files').value=selected;$('solve').disabled=solving||!selected;if(updateStatus)$('status').textContent=`${d.files.length} valid forecast file(s). Scanned ${d.scanned_at}.`;$('fileErrors').innerHTML=d.errors.length?table(['Rejected file','Reason'],d.errors.map(x=>[x.file,x.error])):'';$('methodMap').innerHTML=d.methods.map(x=>`<article class="method-row"><div><h3>${esc(x.method)}</h3><span class="badge ${x.status==='Applied'?'':'partial'}">${esc(x.status)}</span></div><p>${esc(x.application)}</p></article>`).join('');if(before!==selected||!charts.priceChart)await preview();}catch(e){$('status').textContent=e.message;}}
$('files').onchange=()=>{selected=$('files').value;preview().catch(e=>$('status').textContent=e.message);};$('scan').onclick=()=>scan();
$('upload').onchange=async()=>{try{const f=$('upload').files[0];if(!f)return;if(f.size>2_000_000)throw Error('Export exceeds 2 MB');const d=await api('/api/import',JSON.parse(await f.text()));selected=d.file;await scan();await preview();}catch(e){$('status').textContent='Import rejected: '+e.message;}};
function showSchedule(run) {
 const comparable = other => other && other.input_sha256 === run.input_sha256 && other.scenario === run.scenario;
 if (comparable(lastResult) && lastResult.tariff !== run.tariff) comparedResult = lastResult;
 if (!comparable(comparedResult)) comparedResult = null;
 lastResult = run;
 $('resultPlaceholder').hidden = true;
 $('resultBody').hidden = false;
 const saving = run.actual_saving;
 const replayed = saving == null ? 'Replayed cost pending observed prices.'
  : Math.abs(saving) < 0.005 ? 'Replayed saving rounds to AUD 0.00.'
  : saving < 0 ? `The optimized plan cost AUD ${fmt(-saving)} more on replay.`
  : `The optimized plan saved AUD ${fmt(saving)} on replay.`;
 $('runSummary').textContent = `${run.forecast.forecast_origin.slice(0,10)} · ${run.tariff === 'flat' ? 'Flat' : 'Wholesale-linked'} tariff · ${replayed} Both plans passed.`;
 $('runMeta').textContent = `${run.run_id} · ${run.executed_at} · input ${run.input_sha256}`;
 $('costs').innerHTML = table(['Plan','Estimated AUD','Replayed AUD','GPU-hours','Grid kWh','Check'], run.plans.map(plan => [
  plan.name,fmt(plan.forecast_cost),fmt(plan.actual_cost),fmt(plan.check.summary.total_gpu_hours),
  fmt(plan.check.summary.total_grid_energy_kwh),plan.check.verdict
 ]));
 const hedge = run.hedge;
 $('hedgeResult').hidden = !hedge;
 if (hedge) {
  $('hedgeSummary').textContent = `${fmt(hedge.hedged_kw)} kW of base load · ${fmt(hedge.hedged_mwh, 3)} MWh over 24 hours · A$${fmt(hedge.strike_aud_mwh)}/MWh strike`;
  $('hedgeCosts').innerHTML = table(['Price case','Start early AUD','LP only AUD','Hedge payoff AUD','LP + hedge AUD'],
   hedge.scenarios.map(row => [row.scenario,fmt(row.start_early_aud),fmt(row.schedule_aud),
    fmt(row.hedge_payoff_aud),fmt(row.schedule_and_hedge_aud)]));
  $('hedgeNote').textContent = hedge.note;
 }
 $('assumptions').innerHTML = table(['Assumption','Value'], Object.entries(run.assumptions));
 $('checks').innerHTML = table(['Plan','Verdict'], run.plans.map(plan => [plan.name,plan.check.verdict]));
 $('limits').textContent = run.limitations;
 $('compareTariff').textContent = run.tariff === 'linked' ? 'Try the flat tariff' : 'Try the wholesale-linked tariff';
 $('tariffComparison').innerHTML = comparedResult && comparedResult.tariff !== run.tariff
  ? table(['Same case','Estimated saving (AUD)','Replayed saving (AUD)'], [run, comparedResult].map(item => [
      item.tariff === 'flat' ? 'Flat' : 'Wholesale-linked',fmt(item.predicted_saving),fmt(item.actual_saving)
    ])) : '';
 chart('scheduleChart','bar',run.plans[0].plan.schedule.map(slot=>slot.start_iso.slice(11,16)),
  run.plans.map((plan,index)=>({label:plan.name,data:plan.plan.schedule.map(slot=>slot.total_allocation_gpu),backgroundColor:index?'#815436':'#a6ad9b'})));
}
async function runSchedule(inputs) {
 if (solving) return;
 solving = true;
 $('solve').disabled = true;
 $('compareTariff').disabled = true;
 $('status').textContent = 'Solving from forecast prices, then checking both schedules…';
 try {
  showSchedule(await api('/api/solve', inputs));
  $('status').textContent = 'Complete. Both schedules verified and saved to Data/outputs.';
  $('results').scrollIntoView({block:'start'});
 } catch (error) {
  $('status').textContent = 'Run failed: ' + error.message;
  $('status').scrollIntoView({block:'center'});
 } finally {
  solving = false;
  $('solve').disabled = !selected;
  $('compareTariff').disabled = false;
 }
}
$('run').onsubmit = event => {
 event.preventDefault();
 const inputs = {file:selected,scenario:$('scenario').value,tariff:$('tariff').value};
 if ($('hedgeEnabled').checked && inputs.tariff === 'linked') {
  inputs.hedge_strike = $('hedgeStrike').valueAsNumber;
  inputs.hedge_coverage = $('hedgeCoverage').valueAsNumber / 100;
 }
 runSchedule(inputs);
};
function syncHedgeControls() {
 const available = $('tariff').value === 'linked';
 $('hedgeEnabled').disabled = !available;
 $('hedgeStrike').disabled = $('hedgeCoverage').disabled = !available || !$('hedgeEnabled').checked;
}
$('tariff').onchange = $('hedgeEnabled').onchange = syncHedgeControls;
syncHedgeControls();
$('compareTariff').onclick = () => {
 const tariff = lastResult.tariff === 'flat' ? 'linked' : 'flat';
 runSchedule({file:lastResult.input_file,scenario:lastResult.scenario,tariff});
};
$('download').onclick=()=>{if(!lastResult)return;const u=URL.createObjectURL(new Blob([JSON.stringify(lastResult,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download=lastResult.run_id+'.json';a.click();URL.revokeObjectURL(u);};scan();setInterval(()=>scan(false),10000);

$('forecastRun').onsubmit = async event => {
  event.preventDefault();
  $('generateForecast').disabled = true;
  $('forecastStatus').textContent = 'Running both forecasts…';
  $('demandPanel').hidden = true;
  try {
    const result = await api('/api/predict', {
      day: $('forecastDay').value,
      model: $('forecastModel').value
    });
    selected = result.file;
    await scan();
    await preview();
    const demand = result.demand;
    const method = demand.config.selected_model;
    const observations = demand.actual.filter(value => value !== null).length;
    const metric = demand.metrics.find(row => row.method === method);
    $('demandSummary').textContent = `${method.toUpperCase()} · ${observations}/48 observed intervals` +
      (metric ? ` · MAE ${fmt(metric.mae)} MW` : '');
    $('demandPanel').hidden = false;
    chart('demandChart', 'line', demand.labels.map(label => label.slice(11, 16)), [
      {label: 'Forecast MW', data: demand.predictions[method], borderColor: '#815436', pointRadius: 0},
      {label: 'Observed MW', data: demand.actual, borderColor: '#2e625d', pointRadius: 0}
    ]);
    $('forecastStatus').textContent = 'Forecasts saved. The price forecast is selected for scheduling.';
  } catch (error) {
    $('forecastStatus').textContent = error.message;
  } finally {
    $('generateForecast').disabled = false;
  }
};

document.querySelectorAll('details').forEach(details => {
 details.addEventListener('toggle', () => {
  if (details.open) details.querySelectorAll('canvas').forEach(canvas => charts[canvas.id]?.resize());
 });
});
