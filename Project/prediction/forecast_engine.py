from pathlib import Path
from contextlib import closing
import copy,csv,datetime as dt,hashlib,json,sqlite3,threading
from tempfile import NamedTemporaryFile
import numpy as np
import pandas as pd
import torch
from course_models.history import DATA,load
from course_models.train_m3 import sample,tensors,model_forward
from course_models.models import build_model

ROOT=DATA.parent.parent
DEFAULT_RUNS={'full':'m3_20261110T081713Z','recent':'m3_20261110T082820Z'}
ACTIVE_RUNS=DATA/'models'/'active_price_runs.json'
RUNS={**DEFAULT_RUNS, **(json.loads(ACTIVE_RUNS.read_text()) if ACTIVE_RUNS.exists() else {})}
DB=DATA/'market_adapter/nsw1_latest.db'
LOCK=threading.RLock();CACHE={}

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def write_json(path,value):
    body = json.dumps(value, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(body)
            stream.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
def config(window='full'):
    if window not in RUNS:raise ValueError('Choose full or recent training window')
    path=DATA/'outputs/m3'/RUNS[window]
    return path,json.loads((path/'config.json').read_text()),json.loads((path/'metrics.json').read_text())
def history():
    with LOCK:
        if 'history' not in CACHE:CACHE['history']=load()
        return CACHE['history'].copy()
def market_frame():
    frame=history()[['price','demand']];parts=[]
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as con:
        rows=con.execute('SELECT market_ts,rrp_aud_mwh,totaldemand_mw FROM nsw1_5min ORDER BY market_ts').fetchall()
        if rows:
            a=pd.DataFrame(rows,columns=['end','price','demand']);a['priority']=0;parts.append(a)
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE name='dispatch_5min'").fetchone()
        if exists:
            rows=con.execute('SELECT observation_time,rrp_aud_mwh,totaldemand_mw,ingested_at_utc FROM dispatch_5min WHERE datetime(ingested_at_utc)<=datetime(?) ORDER BY ingested_at_utc',(now(),)).fetchall()
            if rows:
                a=pd.DataFrame(rows,columns=['end','price','demand','ingested']);a['priority']=1;parts.append(a)
    if parts:
        a=pd.concat(parts,ignore_index=True);a['end']=pd.to_datetime(a.end)
        cutoff=pd.Timestamp.now(tz='Etc/GMT-10').tz_localize(None)
        a=a[a.end<=cutoff].sort_values(['end','priority'],kind='stable').drop_duplicates('end',keep='last')
        a.index=pd.DatetimeIndex(a.end-pd.Timedelta(minutes=5));complete=[]
        for slot,g in a.groupby(a.index.floor('30min')):
            if g.index.equals(pd.date_range(slot,periods=6,freq='5min')):complete.append((slot,g.price.mean(),g.demand.mean()))
        if complete:
            live=pd.DataFrame(complete,columns=['timestamp','price','demand']).set_index('timestamp')
            frame=pd.concat([frame,live]);frame=frame[~frame.index.duplicated(keep='last')].sort_index()
    return frame

def scalers(window,fg):
    dest=DATA/'models'/f'{RUNS[window]}_{fg}_scalers.json'
    with LOCK:
        if dest.exists():return json.loads(dest.read_text())
        _,cfg,stats=config(window)
        saved=stats.get(f'fnn_{fg}',{}).get('scalers')
        if saved is not None:return saved
        if cfg.get('includes_market_database'):raise ValueError('Candidate is missing its saved training scalers')
        dates=pd.date_range(cfg['splits']['train']['first_origin'],cfg['splits']['train']['last_origin'],freq='D')
        frame=history();samples=[sample(frame,d) for d in dates];samples=[s for s in samples if s is not None]
        x=np.stack([s[0] for s in samples]);y=np.stack([s[3] for s in samples]);c=1 if fg=='FG1' else 2
        sc={'x_mean':x[:,:,:c].mean(axis=(0,1)).tolist(),'x_std':np.maximum(x[:,:,:c].std(axis=(0,1)),1e-6).tolist(),'y_mean':float(y.mean()),'y_std':float(max(y.std(),1e-6)),'train_origins':len(samples),'run_id':RUNS[window],'feature_group':fg,'history_sha256':hashlib.sha256((DATA/'processed/history_30min.csv.gz').read_bytes()).hexdigest(),'provenance':'Deterministically reconstructed using the exact original training sample/scaler definition; saved for subsequent inference.'}
        write_json(dest,sc);return sc

def predict(frame,day,window='full',fg='FG2'):
    if fg not in ['FG1','FG2','FG3']:raise ValueError('Choose FG1, FG2 or FG3')
    origin=pd.Timestamp(day)
    if origin.tzinfo is not None:origin=origin.tz_convert('Etc/GMT-10').tz_localize(None)
    if origin!=origin.floor('30min'):raise ValueError('Forecast origin must be on a half-hour boundary')
    path,cfg,stats=config(window)
    last_validation=pd.Timestamp(cfg['splits']['validation']['last_origin'])
    if origin<last_validation+pd.Timedelta(days=1):raise ValueError(f'Choose an origin after validation ends on {last_validation.date()}')
    s=sample(frame,origin,targets=False)
    if s is None:raise ValueError(f'{day}: incomplete prior 48-hour or previous-week window. Missing observations are not filled. Fetch published data or choose another date.')
    a,c,w,_=s;sc=scalers(window,fg);X,E=tensors(a[None],c[None],w[None],fg,sc);pred={};hashes={};torch.set_num_threads(2)
    for kind in ['fnn','cnn','lstm']:
        key=(RUNS[window],fg,kind)
        with LOCK:
            if key not in CACHE:
                net=build_model(kind,1 if fg=='FG1' else 2,E.shape[1]);weight=path/'weights'/f'{kind}_{fg}_seed0.pt';sha=hashlib.sha256(weight.read_bytes()).hexdigest()
                if sha!=stats[f'{kind}_{fg}']['weight_sha256']:raise ValueError('Saved model hash mismatch')
                net.load_state_dict(torch.load(weight,map_location='cpu',weights_only=True));net.eval();CACHE[key]=net
            net=CACHE[key]
        with torch.no_grad():v=model_forward(net,kind,X,E).numpy()[0]*sc['y_std']+sc['y_mean']
        if not np.isfinite(v).all():raise ValueError('Nonfinite forecast rejected')
        pred[kind]=v.astype(float).tolist();hashes[kind]=stats[f'{kind}_{fg}']['weight_sha256']
    pred['prev-week']=w.astype(float).tolist();pred['prev-day']=frame.reindex(pd.date_range(origin-pd.Timedelta(days=1),periods=48,freq='30min')).price.astype(float).tolist()
    labels=pd.date_range(origin,periods=48,freq='30min');actual=frame.reindex(labels).price.to_numpy(float)
    partial=[float(v) if np.isfinite(v) else None for v in actual];rows=[]
    for kind,v in pred.items():
        mask=np.isfinite(actual);err=np.array(v)[mask]-actual[mask]
        rows.append({'method':kind,'mae':float(np.abs(err).mean()) if len(err) else None,'rmse':float(np.sqrt((err*err).mean())) if len(err) else None,'bias':float(err.mean()) if len(err) else None,'n':int(mask.sum())})
    return {'origin':origin.isoformat()+'+10:00','labels':[x.isoformat()+'+10:00' for x in labels],'predictions':pred,'actual':partial,'complete_actuals':all(x is not None for x in partial),'actual_targets':sum(x is not None for x in partial),'metrics':rows,'model_version':RUNS[window],'weights':hashes,'group':fg,'window':window,'feature_cutoff':origin.isoformat()+'+10:00','training':cfg['splits'],'input_hash':hashlib.sha256(a.tobytes()+c.tobytes()+w.tobytes()).hexdigest(),'timing_note':'Day-ahead replay at nominal midnight using completed prior observations. Execution time is separate. Historical finalized values are not a publication-delay replay; intraday retraining is not performed.'}

def experiments(window='full',fg='FG2'):
    path,cfg,m=config(window);rows=[]
    for k in ['fnn','cnn','lstm']:
        r=m[f'{k}_{fg}'];rows.append({'method':k,**r['test']['overall'],'validation_mae':r['validation_mae']})
    rows += [{'method':k,**v,'validation_mae':None} for k,v in m['baselines'].items()]
    chosen=min(['fnn','cnn','lstm'],key=lambda k:m[f'{k}_{fg}']['validation_mae'])
    return {'version':RUNS[window],'splits':cfg['splits'],'rows':rows,'validation_choice':chosen,'breakdowns':{k:m[f'{k}_{fg}']['test'] for k in ['fnn','cnn','lstm']},'curves':{k:m[f'{k}_{fg}']['curve'] for k in ['fnn','cnn','lstm']},'note':'Daily origins, 48 non-overlapping targets per day; held-out January 2025–October 2026. Seed 0 experiment. Compare identical test targets; do not compare directly with the earlier six-hour-origin experiment.'}


def make_export(f,method='lstm',executed_at=None,kind='historical_replay',output_dir=None):
    if method not in f['predictions']:raise ValueError('Unsupported method')
    generated=executed_at or now()
    rows=[{'target_start':t,'target_end':(pd.Timestamp(t)+pd.Timedelta(minutes=30)).isoformat(),'price':float(p)} for t,p in zip(f['labels'],f['predictions'][method])]
    d={'schema':'electricity.forecast.v2','export_id':hashlib.sha256((generated+f['input_hash']+method).encode()).hexdigest()[:24],'generated_at':generated,'kind':kind,'region':'NSW1','currency':'AUD','price_unit':'AUD/MWh','interval_minutes':30,'forecast_origin':f['origin'],'input_cutoff':f['feature_cutoff'],'model':method,'model_version':f['model_version'],'feature_group':f['group'],'input_hash':f['input_hash'],'weight_sha256':f['weights'].get(method),'slots':rows,'evaluation_actuals':f['actual'],'limitations':f['timing_note']}
    destination = DATA / 'exports' if output_dir is None else output_dir
    write_json(destination/('forecast_'+d['export_id']+'.json'),d)
    return d

def execute(params, output_dir=None, demand_dir=None):
    day = str(params.get('day', '2026-03-11'))
    window = params.get('window', 'full')
    group = params.get('group', 'FG2')
    method = params.get('model', 'lstm')
    frame = market_frame()
    forecast = predict(frame, day, window, group)
    if method == 'auto':
        method = experiments(window, group)['validation_choice']
    demand = None
    if params.get('include_demand'):
        from demand_engine import predict as predict_demand
        demand = predict_demand(day, frame=frame)
    executed = now()
    export = make_export(forecast, method, executed, output_dir=output_dir)
    result = {'id': export['export_id'], 'executed_at': executed, 'forecast': forecast,
              'selected_model': method, 'export': export}
    if demand is not None:
        destination = (demand_dir or output_dir or DATA / 'exports') / f'demand_{export["export_id"]}.json'
        write_json(destination, demand)
        result.update(demand=demand, demand_file=str(destination))
    return result
