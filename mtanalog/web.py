"""Loopback-only read-only chart service, intended behind the existing Caddy."""
import fcntl
import gzip
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import secrets
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .webdata import chart_data, price_settings
from .storage import Store
from .access import COOKIE, permission, digest, read_registry
from .market import start_worker
from .updates import delta
from .metrics import LoadMetrics

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent/'static'


def is_web(pid):
    try:
        parts = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return b'mtanalog' in parts and b'web' in parts
    except OSError:
        return False


def start(config, config_path):
    root = Path(config['data_dir'])
    pid_file = root/'web.pid'
    if pid_file.exists():
        pid = int(pid_file.read_text())
        if is_web(pid):
            return pid
    with (root/'web-launcher.log').open('ab') as log:
        child = subprocess.Popen([sys.executable, '-m', 'mtanalog', '--config', str(config_path), 'web'],
                                 cwd=config_path.parent, start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log)
    time.sleep(1)
    if child.poll() is not None:
        raise RuntimeError('Web service failed to start; inspect web.log')
    return child.pid


def stop(config):
    pid_file = Path(config['data_dir'])/'web.pid'
    if not pid_file.exists():
        return False
    pid = int(pid_file.read_text())
    if not is_web(pid):
        return False
    os.kill(pid, signal.SIGTERM)
    return True


class ServiceBusy(Exception):
    pass


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler, workers=8):
        self.slots = threading.BoundedSemaphore(workers)
        self.metrics = None
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            if self.metrics:self.metrics.observe(503,0,0,rejected=True)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class ChartService:
    def __init__(self, config):
        self.config = config
        self.metrics = LoadMetrics()
        self.cache = {}
        self.lock = threading.Lock()
        self.compute = threading.Lock()
        self.computations = []
        self.cache_bytes = 0
        self.history = {}
        self.history_bytes = 0
        self.patches = {}

    def dataset(self, mode, step=None, margin=None):
        step, margin = price_settings(self.config, mode, step, margin)
        key = (mode, step, margin)
        def cached():
            with self.lock:
                item = self.cache.get(key)
                return item[1] if item and time.monotonic()-item[0] < 15 else None
        body = cached()
        if body is not None:
            return body
        # Waiting requests do not form an unbounded serialized computation queue.
        if not self.compute.acquire(timeout=3):
            raise ServiceBusy()
        try:
            body = cached()
            if body is not None:
                return body
            now = time.monotonic()
            self.computations = [stamp for stamp in self.computations if now-stamp < 60]
            if len(self.computations) >= 30:
                raise ServiceBusy()
            self.computations.append(now)
            value = chart_data(self.config, mode, price_step=step, margin_pct=margin, aligned=True)
            value['revision'] = secrets.token_hex(12)
            body = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
            if len(body) > 32*1024*1024:
                raise ValueError('Chart response too large')
            with self.lock:
                while self.cache and (len(self.cache) >= 16 or self.cache_bytes+len(body) > 64*1024*1024):
                    old = self.cache.pop(next(iter(self.cache)))
                    self.cache_bytes -= len(old[1])
                previous = self.cache.pop(key, None)
                if previous:
                    self.cache_bytes -= len(previous[1])
                self.cache[key] = (time.monotonic(), body)
                self.cache_bytes += len(body)
                while self.history and (len(self.history)>=24 or self.history_bytes+len(body)>32*1024*1024):
                    expired=self.history.pop(next(iter(self.history)))
                    self.history_bytes-=len(expired)
                if len(body)<=32*1024*1024:
                    self.history[value['revision']]=body
                    self.history_bytes+=len(body)
            return body
        finally:
            self.compute.release()

    def update(self, mode, step=None, margin=None, since=None):
        body=self.dataset(mode,step,margin)
        if not since: return body
        current=json.loads(body)
        if current['revision']==since:
            return json.dumps({'type':'unchanged','revision':since}).encode()
        key=(since,current['revision'])
        with self.lock:
            cached=self.patches.get(key)
            previous=self.history.get(since)
        if cached is not None: return cached
        if previous is None: return body
        patch=delta(json.loads(previous),current)
        if patch is None: return body
        encoded=json.dumps(patch,separators=(',',':'),allow_nan=False).encode()
        if len(encoded)>=len(body): return body
        with self.lock:
            while self.patches and (len(self.patches)>=16 or sum(map(len,self.patches.values()))+len(encoded)>8*1024*1024):
                self.patches.pop(next(iter(self.patches)))
            if len(encoded)<=8*1024*1024: self.patches[key]=encoded
        return encoded

    def readiness(self):
        try:
            with_store = Store(self.config['data_dir'], read_only=True)
            try:
                state = with_store.get_meta('collector', {})
                age = max(0, int(time.time()*1000)-state.get('heartbeat_ms', 0))
                lag = state.get('processing_lag_ms', 0)
                ok = state.get('state') == 'collecting' and age < 30000 and lag < 15000
                return ok, {'ok': ok, 'heartbeat_age_ms': age, 'processing_lag_ms': lag,
                            'queue_events': state.get('queue_events', 0)}
            finally:
                with_store.close()
        except Exception:
            return False, {'ok': False}


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def parse_request(self):
            result = super().parse_request()
            self.started = time.monotonic()
            return result

        def log_message(self, *_):
            pass  # Never log links, cookies or POST bodies.

        def reply(self, status, body, content_type='application/json', extra=None):
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                             "base-uri 'none'; form-action 'self'")
            if getattr(self, 'access_expires', None) is not None:
                self.send_header('X-Access-Expires', str(int(self.access_expires)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            if len(body) > 2048 and 'gzip' in self.headers.get('Accept-Encoding', ''):
                body = gzip.compress(body, compresslevel=3)
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                if self.command != 'HEAD':self.wfile.write(body)
            finally:
                path=urlsplit(self.path).path
                if path!='/api/load':
                    client=self.headers.get('X-Chart-Visitor') if path=='/api/chart' and status==200 else None
                    service.metrics.observe(status,(time.monotonic()-self.started)*1000,len(body),client)


        def has_access(self):
            try:
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                token = cookie[COOKIE].value if COOKIE in cookie else ''
            except Exception:
                token = ''
            allowed, self.access_expires = permission(service.config, token)
            return allowed

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/enter':
                return self.reply(200, (STATIC/'access.html').read_bytes(), 'text/html; charset=utf-8')
            if parsed.path == '/healthz':
                return self.reply(200, {'ok': True})
            if parsed.path in ('/access.js', '/style.css', '/montserrat.ttf', '/favicon.svg'):
                name = parsed.path[1:]
                kind = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                        '.ttf': 'font/ttf', '.svg': 'image/svg+xml'}[Path(name).suffix]
                return self.reply(200, (STATIC/name).read_bytes(), kind)
            if not self.has_access():
                if parsed.path == '/':
                    return self.reply(200, (STATIC/'access.html').read_bytes(), 'text/html; charset=utf-8')
                return self.reply(401, {'error': 'Доступ закрыт или срок ссылки истёк.', 'code': 'access_required'},
                                  extra={'Set-Cookie': COOKIE+'=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax'})
            if parsed.path == '/readyz':
                ok, status = service.readiness()
                return self.reply(200 if ok else 503, status)
            if parsed.path == '/api/chart':
                mode = parse_qs(parsed.query).get('mode', ['1d'])[0]
                if mode not in ('1d', '1w'):
                    return self.reply(400, {'error': 'Неизвестный период.'})
                try:
                    query = parse_qs(parsed.query)
                    step = int(query['step'][0]) if 'step' in query else None
                    margin = float(query['range'][0]) if 'range' in query else None
                    since=query.get('since',[None])[0]
                    if since is not None and (len(since)!=24 or any(c not in '0123456789abcdef' for c in since)):
                        raise ValueError('Invalid revision')
                    return self.reply(200, service.update(mode, step, margin, since))
                except ServiceBusy:
                    return self.reply(429, {'error': 'Слишком много запросов. Повторите через несколько секунд.'}, extra={'Retry-After': '5'})
                except ValueError:
                    return self.reply(400, {'error': 'Шаг: кратно 25, от 25 до 2000 USDT. Диапазон: от 0.5 до 20%, шаг 0.5%.'})
                except Exception:
                    LOG.warning('Chart snapshot unavailable')
                    return self.reply(503, {'error': 'Архив временно недоступен. Повторим запрос.'})
            if parsed.path == '/api/load':
                cookie=SimpleCookie(self.headers.get('Cookie',''))
                token=cookie[COOKIE].value if COOKIE in cookie else ''
                owner=read_registry(service.config).get('owner_hash','')
                if not owner or len(token)!=43 or not secrets.compare_digest(owner,digest(token)):
                    return self.reply(403,{'error':'Owner access required'})
                result=service.metrics.snapshot();result['collector']=service.readiness()[1]
                return self.reply(200,result)
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/lightweight-charts.js': ('lightweight-charts.js', 'text/javascript; charset=utf-8'),
                     '/updates.js': ('updates.js', 'text/javascript; charset=utf-8'),
                     '/chart.js': ('chart.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8'),
                     '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                     '/montserrat.ttf': ('montserrat.ttf', 'font/ttf')}
            if parsed.path not in files:
                return self.reply(404, {'error': 'Not found'})
            filename, content_type = files[parsed.path]
            self.reply(200, (STATIC/filename).read_bytes(), content_type)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.close_connection = True
            if urlsplit(self.path).path == '/api/access' and service.config.get('web_access_restricted', False):
                # A JSON POST from the same origin cannot be triggered by an external form.
                origin = urlsplit(self.headers.get('Origin', ''))
                if origin.scheme != 'https' or origin.netloc != self.headers.get('Host', ''):
                    return self.reply(403, {'error': 'Откройте ссылку на этом сайте.'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                        raise ValueError()
                    token = json.loads(self.rfile.read(size)).get('token', '')
                    allowed, expires = permission(service.config, token)
                except (ValueError, AttributeError):
                    return self.reply(400, {'error': 'Некорректная ссылка.'})
                if not allowed:
                    return self.reply(401, {'error': 'Срок ссылки истёк или доступ отменён.'})
                age = max(0, int(expires-time.time())) if expires is not None else 30*86400
                cookie = f'{COOKIE}={token}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Lax'
                return self.reply(200, {'ok': True}, extra={'Set-Cookie': cookie})
            return self.reply(404, {'error': 'Not found'})
    return Handler


def run(config):
    root = Path(config['data_dir'])
    handler = RotatingFileHandler(root/'web.log', maxBytes=2_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().handlers[:] = [handler]
    lock = (root/'web.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    service = ChartService(config)
    server = BoundedHTTPServer(('127.0.0.1', config.get('web_port', 8790)), handler_for(service))
    server.metrics = service.metrics
    server.daemon_threads = True
    server.timeout = 1
    pid_file = root/'web.pid'
    pid_file.write_text(str(os.getpid()))
    stopping = threading.Event()
    market_thread = start_worker(root, stopping)
    metrics_thread=threading.Thread(target=service.metrics.persist,args=(root,stopping,service.readiness),daemon=True)
    metrics_thread.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    LOG.info('Read-only web server started on loopback')
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        stopping.set()
        market_thread.join(timeout=12)
        pid_file.unlink(missing_ok=True)
        lock.close()
