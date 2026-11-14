"""Audited source conversion; no resampling across missing observations."""
from pathlib import Path
import argparse,calendar,csv,datetime as dt,hashlib,io,json,sqlite3,urllib.request
from contextlib import closing
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT.parent/'Data'/'prediction'
RAW=DATA/'raw/historical'
OUT=DATA/'processed/history_30min.csv.gz'
MANIFEST=DATA/'processed/history_manifest.json'
URL='https://www.aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{month}_NSW1.csv'

def convert(raw):
    f=pd.read_csv(io.BytesIO(raw))
    if not {'REGION','SETTLEMENTDATE','RRP','TOTALDEMAND'}<=set(f):raise ValueError('Unexpected AEMO columns')
    if set(f.REGION.dropna())!={'NSW1'}:raise ValueError('NSW1 only')
    end=pd.to_datetime(f.SETTLEMENTDATE,format='%Y/%m/%d %H:%M:%S')
    if end.duplicated().any():raise ValueError('Duplicate timestamps')
    # The final pre-5MS interval ends exactly at the transition midnight.
    steps=np.where(end<=pd.Timestamp('2021-10-01'),30,5)
    start=end-pd.to_timedelta(steps,unit='m')
    vals=f[['RRP','TOTALDEMAND']].apply(pd.to_numeric,errors='raise').to_numpy(float)
    if not np.isfinite(vals).all() or (vals[:,1]<0).any():raise ValueError('Invalid prices/demand')
    x=pd.DataFrame({'price':vals[:,0],'demand':vals[:,1],'minutes':steps},index=pd.DatetimeIndex(start)).sort_index()
    if x.index.duplicated().any():raise ValueError('Overlapping interval starts')
    grouped=x.groupby(x.index.floor('30min'));valid=[]
    for slot,g in grouped:
        expected=pd.date_range(slot,periods=6,freq='5min') if g.minutes.iloc[0]==5 else pd.DatetimeIndex([slot])
        if not g.index.equals(expected) or g.minutes.sum()!=30:continue
        valid.append((slot,float(g.price.mean()),float(g.demand.mean()),int(g.minutes.iloc[0])))
    if not valid:raise ValueError('No complete half-hours')
    return pd.DataFrame(valid,columns=['timestamp','price','demand','source_minutes']).set_index('timestamp'),len(f)

def build():
    frames=[];files=[]
    for p in sorted(RAW.glob('PRICE_AND_DEMAND_*_NSW1.csv')):
        raw=p.read_bytes();f,n=convert(raw);frames.append(f)
        files.append({'file':p.name,'raw_rows':n,'halfhours':len(f),'sha256':hashlib.sha256(raw).hexdigest(),'source_minutes':sorted(f.source_minutes.unique().tolist())})
    if not frames:raise ValueError('No historical CSVs downloaded')
    frame=pd.concat(frames).sort_index()
    if frame.index.duplicated().any():raise ValueError('Overlapping historical files')
    OUT.parent.mkdir(parents=True,exist_ok=True);temp=OUT.with_name('history_30min.tmp.gz');frame.to_csv(temp,index_label='timestamp',compression='gzip');temp.replace(OUT)
    grid=pd.date_range(frame.index.min(),frame.index.max(),freq='30min');missing=grid.difference(frame.index)
    m={'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'requested_start':'2016-11-01','requested_end':'2026-11-01','files':files,'months':len(files),'raw_rows':sum(x['raw_rows'] for x in files),'halfhours':len(frame),'first_start':str(frame.index.min()),'last_end':str(frame.index.max()+pd.Timedelta(minutes=30)),'missing_slots':len(missing),'missing_sample':[str(x) for x in missing[:20]],'ten_years_complete':len(files)==120 and str(frame.index.min())=='2016-11-01 00:00:00' and str(frame.index.max()+pd.Timedelta(minutes=30))=='2026-11-01 00:00:00' and len(missing)==0,'convention':'Pre-2021-10-01: 30-minute interval ends shifted -30m. Later: 5-minute ends shifted -5m, exactly six rows per half-hour. Fixed UTC+10. Raw files retained. Finalized-data replay is not an as-published historical replay.'}
    MANIFEST.write_text(json.dumps(m,indent=2));return m

def load(include_store=False):
    f=pd.read_csv(OUT,index_col='timestamp',parse_dates=True)
    if include_store:
        db=DATA/'market_adapter/nsw1_latest.db'
        if db.exists():
            with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as con:
                rows=con.execute('SELECT market_ts,rrp_aud_mwh,totaldemand_mw FROM nsw1_5min ORDER BY market_ts').fetchall()
            if rows:
                a=pd.DataFrame(rows,columns=['end','price','demand']);a.index=pd.to_datetime(a.end)-pd.Timedelta(minutes=5)
                for slot,g in a.groupby(a.index.floor('30min')):
                    if g.index.equals(pd.date_range(slot,periods=6,freq='5min')):
                        f.loc[slot,['price','demand','source_minutes']]=[g.price.mean(),g.demand.mean(),5]
    return f.sort_index()

def acquire(timeout=20,limit=None):
    RAW.mkdir(parents=True,exist_ok=True);results=[];consecutive_failures=0
    months=pd.date_range('2016-11-01','2026-10-01',freq='MS')
    from concurrent.futures import ThreadPoolExecutor
    pending=[m for m in months if not (RAW/f'PRICE_AND_DEMAND_{m:%Y%m}_NSW1.csv').exists()]
    if limit is not None:pending=pending[:limit]
    def download(month):
        name=f'PRICE_AND_DEMAND_{month:%Y%m}_NSW1.csv';p=RAW/name
        url=URL.format(month=month.strftime('%Y%m'))
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'NSW-electricity-research/3.0'})
            with urllib.request.urlopen(req,timeout=timeout) as response:raw=response.read(5_000_001)
            if len(raw)>5_000_000:raise ValueError('Monthly file exceeds size limit')
            f,n=convert(raw);expected=calendar.monthrange(month.year,month.month)[1]*48
            if len(f)!=expected:raise ValueError(f'Incomplete month: {len(f)}/{expected} slots')
            if f.index.min()!=month or f.index.max()+pd.Timedelta(minutes=30)!=month+pd.offsets.MonthBegin(1):raise ValueError('Wrong month coverage')
            temp=p.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(p)
            print('Downloaded',name,flush=True)
            return {'file':name,'url':url,'status':'downloaded','raw_rows':n,'sha256':hashlib.sha256(raw).hexdigest()}
        except Exception as e:
            print('Failed',name,str(e),flush=True)
            return {'file':name,'url':url,'status':'failed','error':str(e)}
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(download,pending))
    log={'retrieved_at':dt.datetime.now(dt.timezone.utc).isoformat(),'results':results,'missing_months':[f'{x:%Y%m}' for x in months if not (RAW/f'PRICE_AND_DEMAND_{x:%Y%m}_NSW1.csv').exists()]}
    (RAW/'TEN_YEAR_ACQUISITION.json').write_text(json.dumps(log,indent=2));return log

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--download',action='store_true');ap.add_argument('--timeout',type=float,default=20);ap.add_argument('--limit',type=int);a=ap.parse_args()
    if a.download:acquire(a.timeout,a.limit)
    m=build();print(json.dumps({k:v for k,v in m.items() if k!='files'},indent=2))
