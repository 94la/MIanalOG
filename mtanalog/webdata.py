"""Read-only chart snapshots from the time-weighted minute archive."""
from collections import OrderedDict
import threading
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

import numpy as np

from .storage import unpack
from .market import snapshot

PRESETS = {'1d': (1, 5), '1w': (7, 10)}


_BIN_CACHE = OrderedDict()
_BIN_BYTES = 0
_BIN_LOCK = threading.Lock()
_BIN_LIMIT = 32*1024*1024


def grouped_bins(packed, low, rows, step):
    """Cache compact chart aggregates, not much larger decoded order levels."""
    global _BIN_BYTES
    key = (hashlib.sha256(packed).digest(),low,rows,step)
    with _BIN_LOCK:
        cached = _BIN_CACHE.get(key)
        if cached is not None:
            _BIN_CACHE.move_to_end(key)
            return cached
    bins = np.asarray([[price,value] for _,price,value in unpack(packed)],dtype=np.float64).reshape(-1,2)
    indices = (bins[:,0]//(step*100)-low//step).astype(np.int64)
    inside = (indices>=0)&(indices<rows)
    values = np.bincount(indices[inside],weights=bins[inside,1],minlength=rows)
    values.flags.writeable = False
    size = values.nbytes+256
    with _BIN_LOCK:
        if key not in _BIN_CACHE and size <= _BIN_LIMIT:
            while _BIN_CACHE and _BIN_BYTES+size > _BIN_LIMIT:
                _,old = _BIN_CACHE.popitem(last=False)
                _BIN_BYTES -= old.nbytes+256
            _BIN_CACHE[key] = values
            _BIN_BYTES += size
    return values


def price_settings(config, mode, step=None, margin=None):
    if mode not in PRESETS:
        raise ValueError('Unknown timeframe')
    step = config['price_step'] if step is None else step
    margin = PRESETS[mode][1] if margin is None else margin
    base = config.get('base_price_step', 25)
    if (not isinstance(step, int) or not base <= step <= 2000 or step % base
            or not math.isfinite(margin) or not .5 <= margin <= 20
            or not math.isclose(margin*2, round(margin*2), abs_tol=1e-9)):
        raise ValueError('Invalid price settings')
    return step, round(margin*2)/2


def chart_data(config, mode='1d', end_ms=None, price_step=None, margin_pct=None, aligned=False):
    if mode not in PRESETS:
        raise ValueError('Unknown timeframe')
    days, _ = PRESETS[mode]
    step, margin = price_settings(config, mode, price_step, margin_pct)
    end = end_ms or int(time.time() * 1000)
    requested_start = end - days * 86400000
    path = Path(config['data_dir']) / 'archive.sqlite'
    db = sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True, timeout=10)
    try:
        db.execute('BEGIN')
        first = db.execute('SELECT min(minute) FROM liquidity WHERE minute>=? AND minute<?',
                           (requested_start // 60000 * 60000, end)).fetchone()[0]
        if first is None:
            return {'empty': True, 'mode': mode, 'generated_ms': end}
        start = max(requested_start, first)
        actual_end = end
        column_ms = (120000 if mode=='1d' else 900000) if aligned else None
        if aligned:
            start = start//column_ms*column_ms
        prices = db.execute('SELECT minute,time,price FROM prices WHERE minute>=? AND minute<? ORDER BY minute',
                            (start // 60000 * 60000, actual_end)).fetchall()
        if not prices:
            return {'empty': True, 'mode': mode, 'generated_ms': end}
        market = snapshot(config['data_dir'], start, actual_end)
        candles = market['candles']
        minimum = min((c['low'] for c in candles), default=min(p for _, _, p in prices))
        maximum = max((c['high'] for c in candles), default=max(p for _, _, p in prices))
        low = math.floor(minimum * (1-margin/100) / step) * step
        high = math.ceil(maximum * (1+margin/100) / step) * step
        rows = int(round((high-low)/step))
        if aligned:
            end = max(start+column_ms,math.ceil(actual_end/column_ms)*column_ms)
        columns = int((end-start)//column_ms) if aligned else min(960, max(1, math.ceil((end-start)/60000)))
        if rows * columns > 2_000_000:
            raise ValueError('Chart matrix too large')
        edges = np.linspace(start, max(end, start+1), columns+1)
        amounts = np.zeros((columns, rows))
        durations = np.zeros(columns)
        known = np.column_stack((np.full(columns,-np.inf),np.full(columns,np.inf)))
        for minute, covered, lower, upper, packed, begin, finish in db.execute(
                'SELECT minute,covered_ms,low,high,bins,first_ms,last_ms FROM liquidity '
                'WHERE minute>=? AND minute<? ORDER BY minute', (start//60000*60000, end)):
            begin = begin if begin is not None else minute
            finish = finish if finish is not None else minute+covered
            a, b = max(start, begin), min(actual_end, finish)
            if b <= a:
                continue
            grouped = grouped_bins(packed,low,rows,step)
            left = max(0, int(np.searchsorted(edges, a, side='right'))-1)
            right = min(columns-1, int(np.searchsorted(edges, b, side='left'))-1)
            for column in range(left, right+1):
                weight = max(0, min(b, edges[column+1])-max(a, edges[column]))
                weight *= min(1, covered/max(1, finish-begin))
                amounts[column] += grouped*weight
                durations[column] += weight
                known[column,0] = max(known[column,0],lower)
                known[column,1] = min(known[column,1],upper)
        populated = durations > 0
        amounts[populated] /= durations[populated, None]
        # Sparse arrays retain ALL volumes; browser applies the requested filter.
        liquidity = [None if not populated[c] else
                     [[r, round(float(v), 2)] for r, v in enumerate(amounts[c]) if v > .01]
                     for c in range(columns)]
        deltas = np.zeros((columns, 7))
        for minute, cohort, delta in db.execute('SELECT minute,cohort,delta FROM cvd WHERE minute>=? '
                                              'AND minute<?', (start//60000*60000, end)):
            col = min(columns-1, max(0, int(np.searchsorted(edges, minute, side='right'))-1))
            deltas[col, cohort] += delta/1e6
        cvd = np.cumsum(np.column_stack([deltas.sum(axis=1), deltas]), axis=0)
        gaps = db.execute('SELECT kind,start,end FROM gaps WHERE start<? AND '
                          '(end IS NULL OR end>?)', (end, start)).fetchall()
        invalid = min((a for kind, a, _ in gaps if kind == 'cvd'), default=None)
        meta = dict(db.execute('SELECT key,value FROM meta WHERE key IN (\'collector\',\'last_trade_ms\')'))
        last_trade = json.loads(meta.get('last_trade_ms', '0'))
        cvd_rows = [None if (edges[c+1] <= prices[0][0] or edges[c] > last_trade
                            or invalid is not None and edges[c+1] > invalid)
                    else [round(float(v), 2) for v in cvd[c]] for c in range(columns)]
        collector = json.loads(meta.get('collector', '{}'))
        last_price = prices[-1][2]
        return {**market, 'empty': False, 'mode': mode, 'generated_ms': actual_end,
                'column_ms': column_ms, 'observed_ms': actual_end,
                'requested_start_ms': requested_start, 'start_ms': start, 'end_ms': end,
                'price_margin_pct': margin, 'low': low, 'high': high, 'price_step': step,
                'rows': rows, 'columns': columns, 'liquidity': liquidity,
                'coverage': [round(min(1, float(d)/(edges[c+1]-edges[c])), 3)
                             for c, d in enumerate(durations)],
                'known': [None if not populated[c] else list(known[c]) for c in range(columns)],
                'prices': [[timestamp, price] for _, timestamp, price in prices],
                'cvd': cvd_rows, 'gaps': gaps, 'last_price': last_price,
                'price_change_pct': (last_price/prices[0][2]-1)*100,
                'collector': {'state': collector.get('state', 'unknown'),
                              'heartbeat_ms': collector.get('heartbeat_ms', 0),
                              'processing_lag_ms': collector.get('processing_lag_ms', 0),
                              'queue_events': collector.get('queue_events', 0)},
                'default_min_usdt': config['min_liquidity_usdt'],
                'aggregation': 'time-weighted mean', 'refresh_seconds': 15}
    finally:
        db.close()
