"""UI-independent forecast importer, LP scheduler and independent evaluation."""
from pathlib import Path
import copy,datetime as dt,hashlib,json,math
from ai_compute import model,asap,checker,schema
from . import hedge
ROOT=Path(__file__).resolve().parents[2];DATA=ROOT/'Data';SCHEMA='electricity.forecast.v2'
METHODS=[{'method':'Linear programming (Lecture 1 p.24, Lecture 2 p.3)','status':'Applied','application':'Minimize the linear electricity expense of divisible work. Job release/deadline, capacity and grid constraints define feasibility.'},{'method':'Feasible baseline and independent verification','status':'Applied','application':'Compare starting as early as possible with the LP. Independently recompute work, power and expense.'},{'method':'Sensitivity and scenarios','status':'Applied','application':'Compare the same workload under a declared flat tariff and a wholesale-linked tariff. Fuel-price causal sensitivity is not estimated.'},{'method':'AI-assisted development and reusable procedure','status':'Applied','application':'WorkSwarm assisted implementation. The executable import-schedule-check workflow can be reused through CLI and documented instructions.'},{'method':'Electricity cost hedge','status':'Applied','application':'Compare a hypothetical fixed-volume NSW1 contract for difference against the frozen LP schedule under observed prices and stated stress cases.'},{'method':'Live LLM planning agent','status':'Not verified','application':'No provider-backed agent result is claimed. The numerical LP and checker run without an LLM.'}]
def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()
def numeric(x):
 if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x):raise ValueError('Prices must be finite JSON numbers')
 return float(x)
def validate(d):
 if not isinstance(d,dict) or d.get('schema')!=SCHEMA:raise ValueError('Expected an electricity.forecast.v2 export from the prediction study')
 if d.get('region')!='NSW1' or d.get('price_unit')!='AUD/MWh' or d.get('currency')!='AUD' or d.get('interval_minutes')!=30:raise ValueError('Region, currency or interval units do not match the contract')
 for key in ['export_id','model','model_version','generated_at','forecast_origin','input_cutoff','kind']:
  if not isinstance(d.get(key),str) or not d[key]:raise ValueError('Missing provenance: '+key)
 times=[]
 for key in ['generated_at','forecast_origin','input_cutoff']:
  t=dt.datetime.fromisoformat(d[key].replace('Z','+00:00'))
  if t.tzinfo is None:raise ValueError('Provenance timestamps need explicit time zones')
 if dt.datetime.fromisoformat(d['input_cutoff'])>dt.datetime.fromisoformat(d['forecast_origin']):raise ValueError('Input cutoff cannot be after origin')
 slots=d.get('slots')
 is_live=d['kind']=='live_aemo_predispatch'
 if not isinstance(slots,list) or not (16<=len(slots)<=48 if is_live else len(slots)==48):
  raise ValueError('AEMO live plans require 16 to 48 half-hours' if is_live else 'Exactly 48 forecast intervals are required')
 for row in slots:
  a=dt.datetime.fromisoformat(row['target_start']);b=dt.datetime.fromisoformat(row['target_end'])
  if a.tzinfo is None or a.utcoffset()!=dt.timedelta(hours=10) or b.utcoffset()!=dt.timedelta(hours=10) or a.minute not in [0,30] or a.second or a.microsecond or b-a!=dt.timedelta(minutes=30):raise ValueError('Targets must be 30-minute NEM UTC+10 intervals')
  if times and a!=times[-1]+dt.timedelta(minutes=30):raise ValueError('Forecast targets contain a gap, overlap or duplicate')
  numeric(row['price']);times.append(a)
 if times[0]!=dt.datetime.fromisoformat(d['forecast_origin']):raise ValueError('Origin does not match first target')
 actual=d.get('evaluation_actuals',[None]*len(d['slots']))
 if not isinstance(actual,list) or len(actual)!=len(slots):raise ValueError('Evaluation actuals must match the forecast intervals')
 for x in actual:
  if x is not None:numeric(x)
 return d

def discover():
 valid=[];errors=[]
 for p in sorted(DATA.rglob('*.json')):
  if any(x in ['outputs','references','provenance','raw','prediction'] for x in p.relative_to(DATA).parts):continue
  try:
   raw=p.read_bytes()
   if len(raw)>2_000_000:raise ValueError('Export exceeds 2 MB')
   d=validate(json.loads(raw));valid.append({'file':str(p.relative_to(DATA)),'sha256':hashlib.sha256(raw).hexdigest(),'export_id':d['export_id'],'origin':d['forecast_origin'],'model':d['model'],'version':d['model_version'],'kind':d['kind']})
  except (ValueError,KeyError,TypeError) as e:errors.append({'file':str(p.relative_to(DATA)),'error':str(e)})
 return {'presentation_available':(ROOT/'Material'/'Presentation.html').is_file(),'files':valid,'errors':errors,'methods':METHODS,'scanned_at':stamp()}

def load(relative):
 p=(DATA/relative).resolve()
 if DATA.resolve() not in p.parents or not p.is_file():raise ValueError('Choose a forecast file inside Data')
 rel=p.relative_to(DATA.resolve())
 # Data/outputs holds run results. The rule this guard enforces is "never feed a result back
 # in as an input", not "never read from outputs": a forecast export is an input by nature,
 # so a frozen forecast_*.json fixture under outputs is loadable. Anything else under outputs
 # (schedule_*.json, demand_*.json, summary.json) is a result and stays rejected.
 if 'outputs' in rel.parts and not p.name.startswith('forecast_'):raise ValueError('Choose a forecast file inside Data')
 raw=p.read_bytes()
 if len(raw)>2_000_000:raise ValueError('Export exceeds 2 MB')
 return validate(json.loads(raw)),hashlib.sha256(raw).hexdigest()

def packet(d,rates,scenario='standard'):
 origin=dt.datetime.fromisoformat(d['forecast_origin']);end=dt.datetime.fromisoformat(d['slots'][-1]['target_end'])
 jobs=[('batch-a',9,17,6,4),('batch-b',13,23,9,6),('nightly',20,24,10,8)]
 if d['kind']=='live_aemo_predispatch':
  jobs=[('batch-a',0,4,6,5),('batch-b',1,6,9,6),('nightly',2,8,10,8)]
 if scenario=='more-work':jobs=[(i,a,b,w*1.5,g) for i,a,b,w,g in jobs]
 if scenario=='earlier':jobs=[(i,a,b-2,w,g) for i,a,b,w,g in jobs]
 if scenario not in ['standard','more-work','earlier']:raise ValueError('Unknown scenario')
 js=[{'id':i,'release_iso':(origin+dt.timedelta(hours=a)).isoformat(),'deadline_iso':(origin+dt.timedelta(hours=b)).isoformat(),'required_gpu_hours':w,'max_parallel_gpu':g} for i,a,b,w,g in jobs]
 table=[]
 for r,rate in zip(d['slots'],rates):
  a=dt.datetime.fromisoformat(r['target_start']);b=dt.datetime.fromisoformat(r['target_end']);e='24:00' if b.date()>a.date() else b.strftime('%H:%M')
  table.append({'start':a.strftime('%H:%M'),'end':e,'rate_aud_per_kwh':rate})
 return {'schema_version':'1.0','synthetic':True,'label':'Declared computing-workload study','meta':{'check_stale':False},'site':{'id':'declared-site','currency':'AUD','time_zone':'+10:00','interval_minutes':30,'it_base_load_kw':8,'incremental_kw_per_gpu':.45,'pue':1.25,'flex_compute_capacity_gpu':24,'grid_power_cap_kw':40,'budget':None},'window':{'start_iso':origin.isoformat(),'end_iso':end.isoformat()},'tariff':{'kind':'dynamic','effective_date':origin.date().isoformat(),'expiry_date':end.date().isoformat(),'table':table},'jobs':js}

def solve(relative,scenario='standard',tariff='linked', *, hedge_strike=None, hedge_coverage=0.5, out_dir=None, stage=None, log=None):
 d,sha=load(relative)
 return run_export(d,sha,relative,scenario,tariff,hedge_strike=hedge_strike,hedge_coverage=hedge_coverage,out_dir=out_dir,stage=stage,log=log)

def run_export(d,sha,label,scenario='standard',tariff='linked', *, hedge_strike=None, hedge_coverage=0.5, out_dir=None, stage=None, log=None, save=True):
 stage=stage or (lambda name:None)
 log=log or (lambda message:None)
 """Run the full chain on an already-validated export dict.

 Used by solve() and by run_case.py, which builds the forecast in memory and therefore
 cannot go through load() (load() deliberately refuses files under Data/outputs).
 Writes the run JSON under out_dir, or under Data/outputs when out_dir is None.
 """
 if tariff not in ['linked','flat']:raise ValueError('Choose linked or flat tariff')
 if hedge_strike is not None:
  if tariff != 'linked':
   raise ValueError('The hedge example requires the wholesale-linked tariff')
  hedge_strike = numeric(hedge_strike)
  hedge_coverage = numeric(hedge_coverage)
  if not 0 < hedge_coverage <= 1:
   raise ValueError('Hedge coverage must be greater than 0 and at most 1')
 # Actual prices never enter this rate vector or either scheduling algorithm.
 convert=lambda prices:[max(numeric(p)/1000*1.9,.001) for p in prices] if tariff=='linked' else [.30]*len(prices)
 forecast=[row['price'] for row in d['slots']]
 pkt=packet(d,convert(forecast),scenario)
 errs=schema.validate(pkt)
 if errs:raise ValueError('; '.join(errs[:4]))
 frozen=[]
 for name,fun,step in [('Start early',asap.asap_schedule,'asap'),('Minimum forecast cost',model.schedule,'lp')]:
  stage(step)
  plan=fun(copy.deepcopy(pkt))
  if plan.get('status')!='feasible':raise ValueError('Workload infeasible: '+str(plan.get('infeasibility_reasons')))
  stage('check')
  check=checker.evaluate(pkt,plan)
  if check['verdict']!='PASS':raise ValueError('Independent verification failed')
  frozen.append({'name':name,'plan':plan,'check':check,'forecast_cost':check['summary']['total_cost_currency']})
  log('%s: %s forecast cost %.6f AUD, verdict %s'%(step,name,check['summary']['total_cost_currency'],check['verdict']))
 actual=d.get('evaluation_actuals',[None]*len(d['slots']))
 for row in frozen:
  row['actual_cost']=None
  if all(x is not None for x in actual):row['actual_cost']=checker.evaluate(packet(d,convert(actual),scenario),row['plan'])['summary']['total_cost_currency']
 predicted_saving=frozen[0]['forecast_cost']-frozen[1]['forecast_cost'];actual_saving=frozen[0]['actual_cost']-frozen[1]['actual_cost'] if frozen[0]['actual_cost'] is not None else None
 log('saving: forecast %.6f AUD'%predicted_saving+('' if actual_saving is None else ', observed %.6f AUD'%actual_saving))
 r={'run_id':'schedule_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f'),'executed_at':stamp(),'input_file':label,'input_sha256':sha,'forecast':d,'scenario':scenario,'tariff':tariff,'assumptions':{'linked':'max(wholesale AUD/MWh / 1000 * 1.9, 0.001) AUD/kWh','flat':'0.30 AUD/kWh','site':'8 kW base, 0.45 kW/GPU, PUE 1.25, 24 GPU, grid cap 40 kW','job_timing':'Release/deadline offsets relative to the start of the exported 24-hour horizon'},'plans':frozen,'predicted_saving':predicted_saving,'actual_saving':actual_saving,'limitations':'Synthetic workload and tariffs. This is a planning experiment, not an actual bill or a validated fuel-shock sensitivity. Rolling exports may include an already-started nominal interval and are experimental. No model retraining, external market download or LLM request occurs here.'}
 if hedge_strike is not None:
  stage('hedge')
  r['hedge'] = hedge.compare(pkt, frozen, forecast, actual, convert, hedge_strike, hedge_coverage)
  _obs=next((row for row in r['hedge']['scenarios'] if row['scenario']=='Observed replay'),None)
  if _obs:log('hedge: strike %.0f AUD/MWh, observed payoff %.6f AUD, net %.6f AUD'%(hedge_strike,_obs['hedge_payoff_aud'],_obs['schedule_and_hedge_aud']))
 if not save:return r
 stage('write')
 base=Path(out_dir) if out_dir is not None else DATA/'outputs';dest=base/(r['run_id']+'.json');r['output_file']=str(dest);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(json.dumps(r,indent=2,allow_nan=False));log('write: '+str(dest));return r
