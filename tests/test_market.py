import unittest
import threading
from unittest.mock import patch
from urllib.error import HTTPError
from tempfile import TemporaryDirectory
from pathlib import Path
import sqlite3
from mtanalog.market import decode, dominance, snapshot, repair_range, fetch, MarketRequestError, start_worker, SCHEMA

class MarketTests(unittest.TestCase):
    def test_wick_and_aggressor_volume(self):
        row=[0,'100','110','80','105',0,59999,'1000',0,0,'600']
        decoded=decode(row,60000)
        self.assertEqual(decoded[3],80)
        rows=[(n*60000,*decoded[1:7],300000) for n in range(5)]
        flow=dominance(rows,300000,5)
        self.assertEqual(flow['buy_pct'],60)
        self.assertEqual(flow['sell'],2000)
        self.assertFalse(dominance(rows[:-1],300000,5)['available'])
        self.assertFalse(dominance(rows,300000,15)['available'])
    def test_open_candle_excluded(self):
        rows=[(n*60000,100,110,80,105,1000,600,n*60000+1000) for n in range(5)]
        self.assertFalse(dominance(rows,300000,5)['available'])
    def test_invalid_and_read_only_snapshot(self):
        with self.assertRaises(ValueError): decode([0,'100','90','80','105',0,59999,'1000',0,0,'600'],60000)
        with TemporaryDirectory() as root:
            self.assertEqual(snapshot(root,0,60000)['candles'],[])
            db=sqlite3.connect(Path(root)/'market.sqlite');db.execute(SCHEMA)
            db.execute('INSERT INTO candles VALUES(0,100,110,80,105,1000,600,60000)');db.commit();db.close()
            self.assertEqual(snapshot(root,0,60000)['candles'][0]['low'],80)

    def test_bad_cache_does_not_disable_depth_chart(self):
        with TemporaryDirectory() as root:
            (Path(root)/'market.sqlite').write_bytes(b'broken')
            self.assertEqual(snapshot(root,0,60000)['candles'],[])

    def test_offline_open_candle_is_refetched_before_hole(self):
        db=sqlite3.connect(':memory:');db.execute(SCHEMA)
        db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)',[(0,100,110,80,105,1000,600,1000),(180000,100,110,80,105,1000,600,181000)])
        self.assertEqual(repair_range(db,0,240000),(0,59999))
        db.execute('UPDATE candles SET observed=300000 WHERE time=0')
        self.assertEqual(repair_range(db,0,240000),(180000,239999))
        db.execute('UPDATE candles SET observed=300000 WHERE time=180000')
        self.assertEqual(repair_range(db,0,240000),(60000,179999))
        db.close()

    def test_http_retry_after_is_respected(self):
        error=HTTPError('https://example.test',429,'busy',{'Retry-After':'120'},None)
        with patch('mtanalog.market.urlopen',side_effect=error):
            with self.assertRaises(MarketRequestError) as caught: fetch(limit=3)
        self.assertEqual(caught.exception.retry_after,120)

    def test_initialization_error_waits_for_retry_and_can_stop(self):
        with TemporaryDirectory() as root:
            stopping=threading.Event();attempted=threading.Event()
            def fail(*args,**kwargs):
                attempted.set();raise sqlite3.OperationalError('unavailable')
            with patch('mtanalog.market.sqlite3.connect',side_effect=fail):
                thread=start_worker(root,stopping)
                try:
                    self.assertTrue(attempted.wait(2))
                    self.assertTrue(thread.is_alive())
                finally:
                    stopping.set();thread.join(2)
            self.assertFalse(thread.is_alive())
