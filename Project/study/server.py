from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import argparse,json,re,uuid
from . import core
from predict import generate
UI=Path(__file__).parent/'ui'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def send(self,b,kind='application/json',code=200):
  self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'");self.end_headers();self.wfile.write(b)
 def reply(self,d,code=200):self.send(json.dumps(d,allow_nan=False).encode(),code=code)
 def do_GET(self):
  p=urlparse(self.path);q=parse_qs(p.query)
  try:
   if p.path=='/api/imports':self.reply(core.discover());return
   if p.path=='/api/preview':
    d,sha=core.load(q.get('file',[''])[0]);self.reply({'forecast':d,'sha256':sha});return
   if p.path.startswith('/slides/'):
    base=core.ROOT/'Material';f=(base/p.path[len('/slides/'):]).resolve()
    if base.resolve() in f.parents and f.is_file() and f.suffix in ['.html','.css','.js','.png','.svg','.woff','.woff2']:
     import mimetypes;self.send(f.read_bytes(),mimetypes.guess_type(str(f))[0] or 'application/octet-stream');return
   if p.path=='/':self.send((UI/'index.html').read_bytes(),'text/html; charset=utf-8');return
   if p.path in ['/app.js','/app.css','/chart.js']:self.send((UI/p.path[1:]).read_bytes(),'text/css' if p.path.endswith('.css') else 'text/javascript');return
   self.reply({'error':'Not found'},404)
  except (ValueError,KeyError,TypeError,FileNotFoundError) as e:self.reply({'error':str(e)},400)
 def do_POST(self):
  hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
  if self.headers.get('Host') not in hosts or self.headers.get('X-Study')!='1' or (self.headers.get('Origin') and self.headers['Origin'] not in {'http://'+x for x in hosts}):self.reply({'error':'Local same-origin requests only'},403);return
  try:
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<2_000_000:raise ValueError('Request limit exceeded')
   d=json.loads(self.rfile.read(n))
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
def main():
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8156);a=p.parse_args();s=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);print('Optimization study: http://127.0.0.1:'+str(a.port),flush=True)
 try:s.serve_forever()
 except KeyboardInterrupt:s.server_close()
if __name__=='__main__':main()
