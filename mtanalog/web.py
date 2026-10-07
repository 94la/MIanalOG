"""Loopback-only read-only chart service, intended behind the existing Caddy."""
import fcntl
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .webdata import chart_data, price_settings
from .storage import Store

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
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
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
        self.cache = {}
        self.lock = threading.Lock()
        self.compute = threading.Lock()
        self.computations = []
        self.cache_bytes = 0

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
            value = chart_data(self.config, mode, price_step=step, margin_pct=margin)
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
            return body
        finally:
            self.compute.release()

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
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            if len(body) > 2048 and 'gzip' in self.headers.get('Accept-Encoding', ''):
                body = gzip.compress(body, compresslevel=3)
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/healthz':
                return self.reply(200, {'ok': True})
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
                    return self.reply(200, service.dataset(mode, step, margin))
                except ServiceBusy:
                    return self.reply(429, {'error': 'Слишком много запросов. Повторите через несколько секунд.'}, extra={'Retry-After': '5'})
                except ValueError:
                    return self.reply(400, {'error': 'Шаг: кратно 25, от 25 до 2000 USDT. Диапазон: от 0.5 до 20%, шаг 0.5%.'})
                except Exception:
                    LOG.warning('Chart snapshot unavailable')
                    return self.reply(503, {'error': 'Архив временно недоступен. Повторим запрос.'})
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/chart.js': ('chart.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8'),
                     '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                     '/manrope.ttf': ('manrope.ttf', 'font/ttf')}
            if parsed.path not in files:
                return self.reply(404, {'error': 'Not found'})
            filename, content_type = files[parsed.path]
            self.reply(200, (STATIC/filename).read_bytes(), content_type)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.close_connection = True
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
    server.daemon_threads = True
    server.timeout = 1
    pid_file = root/'web.pid'
    pid_file.write_text(str(os.getpid()))
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    LOG.info('Read-only web server started on loopback')
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        pid_file.unlink(missing_ok=True)
        lock.close()
