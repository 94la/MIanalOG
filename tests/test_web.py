import gzip
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
import urllib.request
import urllib.error

from mtanalog.storage import Store
from mtanalog.webdata import chart_data
from mtanalog.web import ChartService, handler_for


def fixture(root):
    minute = 1791352800000
    store = Store(root)
    store.save_minute(minute, [['b', 8400000, 3e6], ['b', 8402500, 3e6],
                               ['a', 8600000, 2e6]], 60000, 83500, 84500)
    store.save_minute(minute+120000, [['b', 8400000, 8e6]], 30000, 83500, 84500)
    store.trade({'a': 1, 'T': minute+30000, 'p': '84000', 'q': '1', 'm': False}, 3, 84000000000)
    store.trade({'a': 2, 'T': minute+140000, 'p': '84100', 'q': '1', 'm': True}, 3, -84100000000)
    store.gap('depth', minute+60000, minute+120000)
    store.meta('collector', {'state': 'collecting', 'heartbeat_ms': minute+150000})
    store.close()
    return minute


class WebDataTests(unittest.TestCase):
    def test_real_volumes_group_before_filter_and_preserve_gaps(self):
        with tempfile.TemporaryDirectory() as root:
            minute = fixture(root)
            config = {'data_dir': root, 'price_step': 200, 'min_liquidity_usdt': 5e6}
            data = chart_data(config, '1d', end_ms=minute+180000)
            self.assertEqual(data['price_margin_pct'], 5)
            self.assertEqual(data['start_ms'], minute)
            self.assertIsNone(data['liquidity'][1])
            row = (84000-data['low'])//200
            self.assertIn([row, 6e6], data['liquidity'][0])
            # Low volumes are retained for browser-side filtering, not discarded.
            self.assertTrue(any(v == 2e6 for _, v in data['liquidity'][0]))
            self.assertAlmostEqual(data['cvd'][2][0], -100, places=2)
            self.assertEqual(data['coverage'][2], .5)
            week = chart_data(config, '1w', end_ms=minute+180000)
            self.assertEqual(week['price_margin_pct'], 10)
            self.assertLess(week['low'], data['low'])
            self.assertEqual(data['gaps'], [('depth', minute+60000, minute+120000)])

    def test_custom_price_bins_range_and_validation(self):
        with tempfile.TemporaryDirectory() as root:
            minute = fixture(root)
            config = {'data_dir': root, 'price_step': 200, 'min_liquidity_usdt': 5e6}
            fine = chart_data(config, end_ms=minute+180000, price_step=25, margin_pct=5)
            coarse = chart_data(config, end_ms=minute+180000, price_step=400, margin_pct=12)
            self.assertEqual(fine['price_step'], 25)
            self.assertEqual(coarse['price_margin_pct'], 12)
            self.assertGreater(coarse['high']-coarse['low'], fine['high']-fine['low'])
            self.assertEqual(sum(v for _, v in fine['liquidity'][0]), sum(v for _, v in coarse['liquidity'][0]))
            self.assertTrue(any(v == 6e6 for _, v in coarse['liquidity'][0]))
            for step, margin in ((0, 5), (33, 5), (2025, 5), (25, float('nan')), (25, .1), (25, 21)):
                with self.assertRaises(ValueError):
                    chart_data(config, end_ms=minute+180000, price_step=step, margin_pct=margin)

    def test_time_weighting_coarse_columns_and_cvd_gap(self):
        with tempfile.TemporaryDirectory() as root:
            minute = fixture(root)
            store = Store(root)
            store.gap('cvd', minute+60000, minute+120000)
            store.close()
            config = {'data_dir': root, 'price_step': 200, 'min_liquidity_usdt': 5e6}
            data = chart_data(config, '1w', end_ms=minute+180000)
            self.assertIsNotNone(data['cvd'][0])
            self.assertIsNone(data['cvd'][1])
            self.assertIsNone(data['cvd'][2])

    def test_coarse_columns_weight_time_and_not_number_of_records(self):
        with tempfile.TemporaryDirectory() as root:
            minute = fixture(root)
            store = Store(root)
            store.save_minute(minute+60000, [['b', 8400000, 8e6]], 30000, 83500, 84500)
            store.close()
            config = {'data_dir': root, 'price_step': 200, 'min_liquidity_usdt': 5e6}
            data = chart_data(config, '1w', end_ms=minute+1200*60000)
            # 75-second column: 60 seconds at 6M, then 15 seconds at 8M.
            row = (84000-data['low'])//200
            self.assertIn([row, 6.4e6], data['liquidity'][0])
            self.assertEqual(data['columns'], 960)


class HTTPTests(unittest.TestCase):
    def test_public_api_static_allowlist_and_removed_auth(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            config = {'data_dir': root, 'price_step': 200, 'min_liquidity_usdt': 5e6}
            service = ChartService(config)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(url+'/') as response:
                    html = response.read()
                    self.assertIn(b'BTC', html)
                    self.assertNotIn(b'id="login"', html)
                    self.assertNotIn(b'id="logout"', html)
                    self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                with urllib.request.urlopen(url+'/manrope.ttf') as response:
                    self.assertEqual(response.headers['Content-Type'], 'font/ttf')
                    self.assertGreater(len(response.read()), 10000)
                for path in ('/data/telegram-secret.json', '/../../config.toml'):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(url+path)
                    self.assertEqual(error.exception.code, 404)
                for path in ('/api/auth', '/api/logout'):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(url+path, data=b'{}'))
                    self.assertEqual(error.exception.code, 404)
                with urllib.request.urlopen(url+'/api/chart?mode=1w') as response:
                    self.assertEqual(response.status, 200)
                    self.assertIsNone(response.headers.get('Set-Cookie'))
                request = urllib.request.Request(url+'/api/chart?mode=1d',
                    headers={'Accept-Encoding': 'gzip'})
                with urllib.request.urlopen(request) as response:
                    data = response.read()
                    if response.headers.get('Content-Encoding') == 'gzip':
                        data = gzip.decompress(data)
                    self.assertFalse(json.loads(data)['empty'])
                with urllib.request.urlopen(url+'/api/chart?mode=1d&step=100&range=3') as response:
                    custom = json.loads(response.read())
                    self.assertEqual(custom['price_step'], 100)
                    self.assertEqual(custom['price_margin_pct'], 3)
                for query in ('step=33', 'range=nan', 'range=50', 'step=oops'):
                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        urllib.request.urlopen(url+'/api/chart?'+query)
                    self.assertEqual(invalid.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url+'/api/chart?mode=2w'))
                self.assertEqual(error.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
