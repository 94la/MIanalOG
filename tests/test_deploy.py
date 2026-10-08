import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location('install_web', Path(__file__).parents[1]/'deploy/install-web.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class DeploymentTests(unittest.TestCase):
    def test_portable_unit_escapes_paths_and_has_no_server_identity(self):
        text = installer.unit_text(Path('/srv/chart % instance'), 'chartuser', 'chartgroup')
        self.assertIn('User=chartuser', text)
        self.assertIn('WorkingDirectory=/srv/chart %% instance', text)
        self.assertIn('ExecStart="/srv/chart %% instance/.venv/bin/python"', text)
        self.assertNotIn('/home/research', text)
        self.assertNotIn('193-23', text)

    def test_health_waits_for_delayed_listener(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        server = HTTPServer(('127.0.0.1', port), Handler, bind_and_activate=False)
        def later():
            time.sleep(.1)
            server.server_bind()
            server.server_activate()
            server.serve_forever()
        thread = threading.Thread(target=later, daemon=True)
        thread.start()
        try:
            installer.wait_for_health(f'http://127.0.0.1:{port}/healthz', timeout=2, interval=.02)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
