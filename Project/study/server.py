from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import argparse,hashlib,json,re,threading,time,uuid
from . import core, workbench, live_market
from predict import generate
UI=Path(__file__).parent/'ui'
DEFAULT_PORT = 8156
WORKBENCH_ID = hashlib.sha256(str(core.ROOT.resolve()).encode()).hexdigest()
JOBS={};JOBS_LOCK=threading.Lock();MAX_JOBS=20
LOGS=core.DATA/'app-logs'
def start_job(body):
 job_id=uuid.uuid4().hex[:12]
 job={'id':job_id,'status':'queued','stage':'queued','started':time.time(),'finished':None,
      'result':None,'error':None,'body':body,'exit_code':None,'log':[],
      'log_path':str(LOGS/(job_id+'.log')),
      'command':'run_case.py chain (in-process, study.workbench.run_case)'}
 with JOBS_LOCK:
  JOBS[job_id]=job
  while len(JOBS)>MAX_JOBS:
   oldest=min(JOBS.values(),key=lambda j:j['started'])
   if oldest['status'] in ('queued','running'):break
   JOBS.pop(oldest['id'],None)
 threading.Thread(target=run_job,args=(job,),daemon=True).start()
 return {'job_id':job_id,'status':'queued','stage':'queued','log_path':job['log_path']}
def run_job(job):
 job['status']='running'
 LOGS.mkdir(parents=True,exist_ok=True)
 stream=open(job['log_path'],'w',encoding='utf8')
 def log(message):
  line=time.strftime('%H:%M:%S')+' '+str(message)
  stream.write(line+'\n');stream.flush()
  job['log'].append(line)
  if len(job['log'])>400:del job['log'][:100]
 def stage(name):
  job['stage']=name
  log('['+name+']')
 try:
  job['result']=workbench.run_case(job['body'],log=log,stage=stage)
  job['status']='done';job['exit_code']=0;log('done: exit 0')
 except Exception as e:
  job['error']=str(e);job['status']='error';job['exit_code']=1;log('failed: '+str(e))
 finally:
  job['finished']=time.time()
  try:stream.close()
  except Exception:pass
def job_view(job_id):
 with JOBS_LOCK:job=JOBS.get(job_id)
 if job is None:raise ValueError('Unknown job.')
 now=job['finished'] or time.time()
 return {'job_id':job['id'],'status':job['status'],'stage':job['stage'],
         'elapsed_s':round(now-job['started'],3),'exit_code':job['exit_code'],
         'log_path':job['log_path'],'log_tail':job['log'][-40:],
         'command':job['command'],
         'result':job['result'] if job['status']=='done' else None,'error':job['error']}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def send(self,b,kind='application/json',code=200):
  self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'");self.end_headers();self.wfile.write(b)
 def reply(self,d,code=200):self.send(json.dumps(d,allow_nan=False).encode(),code=code)
 def do_GET(self):
  p=urlparse(self.path);q=parse_qs(p.query)
  try:
   if p.path=='/api/live/dispatch':self.reply(live_market.observation());return
   if p.path=='/api/planning-evaluation':self.reply(workbench.planning_evaluation());return
   if p.path=='/api/health':self.reply({'workbench':WORKBENCH_ID,'solver':core.model.SOLVER});return
   if p.path=='/api/reference':self.reply(workbench.reference_case());return
   if p.path=='/api/market':self.reply(workbench.market_day(q.get('day',[''])[0]));return
   if p.path=='/api/imports':self.reply(core.discover());return
   if p.path=='/api/fixtures':self.reply(workbench.fixtures());return
   if p.path=='/api/provenance':self.reply(workbench.provenance(q.get('day',[''])[0]));return
   if p.path=='/api/replay':self.reply(workbench.replay_day(q.get('day',[''])[0]));return
   if p.path=='/api/jobs':self.reply(job_view(q.get('id',[''])[0]));return
   if p.path=='/api/preview':
    d,sha=core.load(q.get('file',[''])[0]);self.reply({'forecast':d,'sha256':sha});return
   if p.path.startswith('/slides/'):
    base=core.ROOT/'Material';f=(base/p.path[len('/slides/'):]).resolve()
    if base.resolve() in f.parents and f.is_file() and f.suffix in ['.html','.css','.js','.png','.svg','.woff','.woff2']:
     import mimetypes;self.send(f.read_bytes(),mimetypes.guess_type(str(f))[0] or 'application/octet-stream');return
   if p.path=='/':self.send((UI/'index.html').read_bytes(),'text/html; charset=utf-8');return
   if p.path in ['/app.js','/app.css','/chart.js','/favicon.svg']:
    kind='text/css' if p.path.endswith('.css') else ('image/svg+xml' if p.path.endswith('.svg') else 'text/javascript')
    self.send((UI/p.path[1:]).read_bytes(),kind);return
   self.reply({'error':'Not found'},404)
  except (ValueError,KeyError,TypeError,FileNotFoundError) as e:self.reply({'error':str(e)},400)
 def do_POST(self):
  hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
  if self.headers.get('Host') not in hosts or self.headers.get('X-Study')!='1' or (self.headers.get('Origin') and self.headers['Origin'] not in {'http://'+x for x in hosts}):self.reply({'error':'Local same-origin requests only'},403);return
  try:
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<2_000_000:raise ValueError('Request limit exceeded')
   d=json.loads(self.rfile.read(n))
   if self.path=='/api/live/plan':self.reply(live_market.plan(d));return
   if self.path=='/api/workbench':
    self.reply(workbench.run_case(d));return
   if self.path=='/api/jobs':
    self.reply(start_job(d));return
   if self.path=='/api/predict':
    self.reply(generate(d['day'],d.get('model','lstm')));return
   if self.path=='/api/solve':
    self.reply(core.solve(d['file'], d.get('scenario','standard'), d.get('tariff','linked'),
                          hedge_strike=d.get('hedge_strike'), hedge_coverage=d.get('hedge_coverage', 0.5)));return
   if self.path=='/api/import':
    export=core.validate(d);dest=core.DATA/'imports'/('forecast_'+uuid.uuid4().hex+'.json');dest.write_text(json.dumps(export,indent=2,allow_nan=False));self.reply({'file':str(dest.relative_to(core.DATA))});return
   self.reply({'error':'Not found'},404)
  except (ValueError,KeyError,TypeError) as e:self.reply({'error':str(e)},400)
  except Exception as e:self.reply({'error':str(e)},500)
def create_server(port=DEFAULT_PORT):
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(server):
    with server:
        print(f'Optimization study: http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    serve(create_server(parser.parse_args().port))


if __name__ == '__main__':
    main()
