import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mtanalog.collector import collect, RestError
from mtanalog.replay import verify
from mtanalog.storage import Store, unpack


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_buffer_trade_gap_recovery_and_replay(self):
        with tempfile.TemporaryDirectory() as root:
            config = {'data_dir': root, 'base_price_step': 25, 'symbol': 'BTCUSDT',
                      'checkpoint_seconds': 3600, 'retention_days': 14, 'min_free_gib': 0}
            stamp = int(time.time()*1000)
            def trade(identifier, price='1000'):
                return {'e': 'aggTrade', 'a': identifier, 'p': price, 'q': '1',
                        'm': False, 'T': stamp}
            async def feed(config, queue, stop):
                for item in [trade(10),
                    {'e': 'depthUpdate', 'U': 101, 'u': 101, 'b': [['100', '8']], 'a': []},
                    trade(12),
                    {'e': 'depthUpdate', 'U': 102, 'u': 102, 'b': [], 'a': [['1000', '50']]}]:
                    queue.put_nowait((stamp, item))
                await stop.wait()
            def fake_rest(config, path, **params):
                if path.endswith('depth'):
                    return {'lastUpdateId': 100, 'bids': [['100', '1']], 'asks': [['110', '1']]}
                return [trade(11), trade(12)]
            with patch('mtanalog.collector.receive', feed), patch('mtanalog.collector.rest', fake_rest):
                await collect(config, duration=.05)
            store = Store(root)
            self.assertEqual(store.get_meta('last_trade_id'), 12)
            self.assertEqual(store.db.execute('SELECT sum(count) FROM cvd').fetchone()[0], 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM gaps WHERE kind='cvd'").fetchone()[0], 0)
            store.close()
            result = verify(config)
            self.assertTrue(result['book_matches_checkpoint'])
            self.assertTrue(result['cvd_matches_sqlite'])
            self.assertEqual(result['unique_trades'], 3)

    async def test_transient_backfill_error_does_not_leave_false_cvd_gap(self):
        with tempfile.TemporaryDirectory() as root:
            config = {'data_dir': root, 'base_price_step': 25, 'symbol': 'BTCUSDT',
                      'checkpoint_seconds': 3600, 'retention_days': 14, 'min_free_gib': 0}
            stamp = int(time.time()*1000)
            calls = 0
            def trade(identifier):
                return {'e': 'aggTrade', 'a': identifier, 'p': '100', 'q': '1',
                        'm': False, 'T': stamp}
            async def feed(config, queue, stop):
                queue.put_nowait((stamp, trade(10)))
                queue.put_nowait((stamp, trade(12)))
                await stop.wait()
            def fake_rest(config, path, **params):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RestError('temporary', retry_after=0)
                return [trade(11), trade(12)]
            # Retry backoff stays respected; this integration test spans it explicitly.
            with patch('mtanalog.collector.receive', feed), patch('mtanalog.collector.rest', fake_rest):
                await collect(config, duration=1.2)
            store = Store(root)
            self.assertEqual(store.get_meta('last_trade_id'), 12)
            self.assertEqual(store.db.execute("SELECT count(*) FROM gaps WHERE kind='cvd'").fetchone()[0], 0)
            store.close()

    async def test_delayed_backfill_does_not_extend_buffered_wall_across_minutes(self):
        with tempfile.TemporaryDirectory() as root:
            base = int(time.time()*1000)//60000*60000
            config = {'data_dir': root, 'base_price_step': 25, 'symbol': 'BTCUSDT',
                      'checkpoint_seconds': 3600, 'retention_days': 14, 'min_free_gib': 0}
            book = {'lastUpdateId': 100, 'bids': [['100', '10']], 'asks': [['200', '1']]}
            store = Store(root)
            store.checkpoint(book, base, 100, 200)
            store.raw('snapshot', book, base)
            store.close()
            def trade(i, offset):
                return {'e': 'aggTrade', 'a': i, 'p': '100', 'q': '1', 'm': False, 'T': base+offset}
            async def feed(config, queue, stop):
                events = [(0, trade(10, 0)),
                          (0, {'e':'depthUpdate','U':101,'u':101,'b':[],'a':[]}),
                          (1000, trade(12,1000)),
                          (10000, {'e':'depthUpdate','U':102,'u':102,'b':[['100','0']],'a':[]}),
                          (60000, {'e':'depthUpdate','U':103,'u':103,'b':[],'a':[]}),
                          (90000, {'e':'depthUpdate','U':104,'u':104,'b':[],'a':[]})]
                for offset,event in events:
                    queue.put_nowait((base+offset,event))
                await stop.wait()
            def rest(config,path,**params):
                time.sleep(.02)  # Depth stays buffered during REST trade recovery.
                return [trade(11,500),trade(12,1000)]
            with patch('mtanalog.collector.receive',feed), patch('mtanalog.collector.rest',rest), \
                 patch('mtanalog.collector.now_ms',return_value=base+90000):
                await collect(config,duration=.08)
            store = Store(root,read_only=True)
            first=store.load_minute(base)
            bid=next(value for side,price,value in first[0] if side=='b')
            self.assertAlmostEqual(bid,1000*10000/60000)
            second=store.load_minute(base+60000)
            self.assertFalse(any(side=='b' for side,price,value in second[0]))
            self.assertEqual(store.get_meta('last_trade_id'),12)
            store.close()


if __name__ == '__main__':
    unittest.main()
