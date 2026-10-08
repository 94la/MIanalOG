"""Independent public Binance minute-candle cache; never writes the depth archive."""
import fcntl
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.request import urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

MINUTE = 60000
SCHEMA = '''CREATE TABLE IF NOT EXISTS candles(time INTEGER PRIMARY KEY,open REAL,high REAL,low REAL,close REAL,quote REAL,buy REAL,observed INTEGER);'''


def decode(row, observed):
    stamp = int(row[0])
    o, h, l, c, q, b = [float(row[i]) for i in (1, 2, 3, 4, 7, 10)]
    if (stamp % MINUTE or not all(math.isfinite(v) for v in (o,h,l,c,q,b))
            or not 0 < l <= min(o,c) <= max(o,c) <= h or not 0 <= b <= q):
        raise ValueError('Invalid Binance candle')
    return stamp,o,h,l,c,q,b,observed


class MarketRequestError(Exception):
    def __init__(self, retry_after):
        super().__init__('Public Binance request unavailable')
        self.retry_after = retry_after


def fetch(start=None, end=None, limit=1000):
    params = dict(symbol='BTCUSDT', interval='1m', limit=limit)
    if start is not None: params['startTime'] = start
    if end is not None: params['endTime'] = end
    try:
        with urlopen('https://data-api.binance.vision/api/v3/klines?'+urlencode(params), timeout=10) as response:
            rows = json.load(response)
    except HTTPError as error:
        try: delay = max(0, float(error.headers.get('Retry-After', '300' if error.code in (418,429) else '30')))
        except (ValueError,TypeError): delay = 300
        if not math.isfinite(delay): delay = 300
        raise MarketRequestError(delay) from error
    if not isinstance(rows, list): raise ValueError('Invalid Binance response')
    now = int(time.time()*1000)
    return [decode(row, now) for row in rows]


def dominance(rows, end, minutes):
    selected = [r for r in rows if end-minutes*MINUTE <= r[0] < end and r[7] >= r[0]+MINUTE]
    if len(selected) != minutes or [r[0] for r in selected] != list(range(end-minutes*MINUTE,end,MINUTE)):
        return {'available': False, 'minutes': minutes, 'end_ms': end}
    buy = math.fsum(r[6] for r in selected); total = math.fsum(r[5] for r in selected)
    return dict(available=total>0, minutes=minutes, end_ms=end, buy=buy, sell=total-buy,
                delta=2*buy-total, buy_pct=100*buy/total if total else None)


def snapshot(root, start, end):
    path = Path(root)/'market.sqlite'
    result = {'candles': [], 'flow': {}, 'market_updated_ms': None}
    if not path.exists(): return result
    db = None
    try:
        db = sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True,timeout=2)
        db.execute('BEGIN')
        finish = end//MINUTE*MINUTE
        rows = db.execute('SELECT * FROM candles WHERE time>=? AND time<? ORDER BY time',
                          (min(start//MINUTE*MINUTE,finish-60*MINUTE),end)).fetchall()
        result['candles'] = [dict(time=r[0]//1000,open=r[1],high=r[2],low=r[3],close=r[4]) for r in rows if r[0]>=start//MINUTE*MINUTE]
        result['flow'] = {str(n):dominance(rows,finish,n) for n in (5,15,60)}
        result['market_updated_ms'] = rows[-1][7] if rows else None
        return result
    except sqlite3.Error:
        logging.getLogger(__name__).warning('Candle cache unavailable; depth archive remains readable')
        return {'candles': [], 'flow': {}, 'market_updated_ms': None}
    finally:
        if db is not None: db.close()


def repair_range(db, cutoff, now):
    # A candle observed while open must be fetched once more after its close,
    # including the last candle before an offline gap.
    unfinished = db.execute('SELECT time FROM candles WHERE time>=? AND time<? AND observed<time+? ORDER BY time LIMIT 1',
                            (cutoff,now//MINUTE*MINUTE,MINUTE)).fetchone()
    if unfinished: return unfinished[0], unfinished[0]+MINUTE-1
    hole = db.execute('SELECT time,next FROM (SELECT time,lead(time) OVER (ORDER BY time) AS next FROM candles WHERE time>=?) WHERE next-time>? LIMIT 1',
                      (cutoff,MINUTE)).fetchone()
    if hole: return hole[0]+MINUTE,min(hole[1]-1,hole[0]+1000*MINUTE)
    earliest = db.execute('SELECT min(time) FROM candles WHERE time>=?',(cutoff,)).fetchone()[0]
    if earliest is not None and earliest>cutoff: return max(cutoff,earliest-1000*MINUTE),earliest-1
    return None


def start_worker(root, stopping):
    def worker():
        lock = (Path(root)/'market.lock').open('a')
        db = None
        try:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                logging.getLogger(__name__).warning('Candle writer already running')
                return
            while db is None and not stopping.is_set():
                try:
                    candidate = sqlite3.connect(Path(root)/'market.sqlite',timeout=10)
                    try:
                        candidate.execute('PRAGMA journal_mode=WAL');candidate.execute(SCHEMA);candidate.commit()
                    except sqlite3.Error:
                        candidate.close()
                        raise
                    db = candidate
                except sqlite3.Error:
                    logging.getLogger(__name__).warning('Candle cache initialization failed; retry in 30 seconds')
                    stopping.wait(30)
            retry = 15
            while not stopping.is_set():
                try:
                    latest = fetch(limit=3 if db.execute('SELECT count(*) FROM candles').fetchone()[0] else 1000)
                    db.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?)',latest);db.commit()
                    cutoff = int(time.time()*1000)//MINUTE*MINUTE-14*86400000
                    repair = repair_range(db, cutoff, int(time.time()*1000))
                    if repair:
                        repaired = fetch(*repair)
                        db.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?)',repaired)
                    db.execute('DELETE FROM candles WHERE time<?',(cutoff,));db.commit()
                    retry = 15
                except Exception as error:
                    db.rollback()
                    logging.getLogger(__name__).warning('Public candle refresh unavailable; retaining cache')
                    retry = max(min(300,retry*2),getattr(error,'retry_after',0))
                stopping.wait(retry)
        finally:
            if db: db.close()
            lock.close()
    thread = threading.Thread(target=worker,name='market-cache',daemon=True);thread.start()
    return thread
