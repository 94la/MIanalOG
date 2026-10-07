import tempfile
import threading
import urllib.request
import http.client
import socket
from http.server import BaseHTTPRequestHandler
import unittest
from unittest.mock import patch

from mtanalog.storage import Store
from mtanalog.web import ChartService, ServiceBusy, BoundedHTTPServer
from mtanalog.webdata import price_settings


class APILimitsTests(unittest.TestCase):
    def test_settings_are_canonical_and_excess_precision_is_rejected(self):
        config={'price_step':200}
        self.assertEqual(price_settings(config,'1d'),(200,5))
        with self.assertRaises(ValueError):price_settings(config,'1d',200,5.0001)
        service=ChartService(config)
        with patch('mtanalog.web.chart_data',return_value={'empty':True}) as calculate:
            first=service.dataset('1d')
            self.assertEqual(first,service.dataset('1d',200,5))
            calculate.assert_called_once()

    def test_compute_rate_is_bounded_and_cached_data_still_works(self):
        service=ChartService({'price_step':200})
        with patch('mtanalog.web.chart_data',return_value={'empty':True}):
            for index in range(30):service.dataset('1d',200,(index+1)/2)
            with self.assertRaises(ServiceBusy):service.dataset('1d',200,15.5)
            self.assertIsInstance(service.dataset('1d',200,15),bytes)
        self.assertLessEqual(len(service.cache),16)
        self.assertLessEqual(service.cache_bytes,64*1024*1024)

    def test_cached_chart_is_available_while_another_chart_is_computing(self):
        service=ChartService({'price_step':200})
        with patch('mtanalog.web.chart_data',return_value={'empty':True}):service.dataset('1d')
        entered=threading.Event();finish=threading.Event();errors=[]
        def slow(*args,**kwargs):
            entered.set();finish.wait(5);return {'empty':True}
        def request():
            try:service.dataset('1w')
            except Exception as error:errors.append(error)
        with patch('mtanalog.web.chart_data',side_effect=slow):
            worker=threading.Thread(target=request);worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIsInstance(service.dataset('1d'),bytes)
            finally:finish.set();worker.join(5)
        self.assertEqual(errors,[])

    def test_readiness_distinguishes_running_from_stale_or_backlogged(self):
        with tempfile.TemporaryDirectory() as root:
            writer=Store(root)
            for state,expected in [({'state':'collecting','heartbeat_ms':100000,'processing_lag_ms':0},True),
                                   ({'state':'collecting','heartbeat_ms':100000,'processing_lag_ms':20000},False),
                                   ({'state':'stopped','heartbeat_ms':100000},False)]:
                writer.meta('collector',state);writer.flush()
                with patch('mtanalog.web.time.time',return_value=101):
                    self.assertEqual(ChartService({'data_dir':root}).readiness()[0],expected)
            writer.close()


class WorkerLimitTests(unittest.TestCase):
    def test_parallel_connections_are_bounded_and_slot_is_released(self):
        entered=threading.Event();finish=threading.Event();outcomes=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                entered.set();finish.wait(3)
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
        server=BoundedHTTPServer(('127.0.0.1',0),Handler,workers=1)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        def first_request():
            with urllib.request.urlopen(url,timeout=5) as response:outcomes.append(response.status)
        first=threading.Thread(target=first_request);first.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError,urllib.error.URLError)):
                urllib.request.urlopen(url,timeout=2)
            finish.set();first.join(5)
            self.assertEqual(outcomes,[200])
            with urllib.request.urlopen(url,timeout=5) as response:self.assertEqual(response.status,200)
        finally:
            finish.set();first.join(5);server.shutdown();server.server_close();thread.join(5)
